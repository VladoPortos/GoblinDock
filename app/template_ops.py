"""Rebuild revision selection and versioned, credential-free template bundles.

Imports resolve an existing local image and explicit target/network mappings.
Image URLs are descriptive metadata only: importing never creates a download source.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session, select

from .db import get_session
from .deps import current_user
from .execution_plan import materialize_execution_plan, open_execution_plan
from .models import Block, Deployment, Image, IpAllocation, Network, Template, User
from .recipes import (ensure_placement_ids, input_schema_problems, is_deployer_secret_ref,
                      lint_block, load_recipe, normalize_input_schema)
from .security import decrypt
from . import statebus

router = APIRouter(prefix="/api")


class RebuildBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["original", "current"] = "original"
    deployInputs: dict = Field(default_factory=dict)


def _visible_template(session, template_id, user):
    tpl = session.get(Template, template_id) if template_id else None
    if not tpl or not (tpl.public or tpl.owner_id == user.id or user.role == "admin"):
        raise HTTPException(404, "template not found")
    return tpl


def read_only_network_ctx(session: Session, net: Optional[Network], dep_id=None) -> dict:
    """Describe an existing or available reservation without inserting/updating it."""
    from . import api
    if not net:
        return {"network_mode": "dhcp"}
    ctx = {"network_mode": net.mode}
    if net.mode == "static":
        pool = api._static_pool(net)
        rows = session.exec(select(IpAllocation).where(IpAllocation.network_id == net.id)).all()
        own = next((row for row in rows if dep_id is not None and row.deployment_id == dep_id), None)
        address = None
        if own and own.state == "reserved":
            try:
                candidate = ipaddress.ip_address(own.ip)
                if (candidate.version == pool.network.version and pool.start <= candidate <= pool.end
                        and not pool.is_reserved(candidate)):
                    address = candidate
            except ValueError:
                pass
        if address is None:
            taken = set()
            for row in rows:
                if own is not None and row.id == own.id:
                    continue
                try:
                    taken.add(ipaddress.ip_address(row.ip))
                except ValueError:
                    pass
            address = next((candidate for candidate in pool.iter_usable() if candidate not in taken), None)
        if address is None:
            raise HTTPException(409, "static IP pool exhausted")
        ctx["static_ip"] = str(address)
        ip_key, gateway_key = ("ip6", "gw6") if address.version == 6 else ("ip", "gw")
        ctx["ipconfig0"] = f"{ip_key}={address}/{pool.network.prefixlen}"
        if pool.gateway is not None:
            ctx["ipconfig0"] += f",{gateway_key}={pool.gateway}"
    for key in ("bridge", "vlan", "dns"):
        value = getattr(net, key)
        if value:
            ctx[key] = value
    return ctx


def prepare_rebuild(session: Session, dep: Deployment, user: User, mode: str = "original",
                    supplied: Optional[dict] = None, *, read_only: bool = False) -> tuple[str, str, str]:
    """Resolve one revision without changing deployment status or original snapshot.

    Callers hold admission locks for mutations. Preview/preflight use read_only=True
    so a legacy static-network check never reserves or repairs an address.
    """
    from . import api
    if mode == "original":
        if supplied:
            raise HTTPException(400, "original rebuild uses its saved answers; choose current to supply new answers")
        if not dep.original_execution_plan_enc or not dep.original_context_enc:
            raise HTTPException(409, "original plan unavailable for this legacy deployment — explicitly choose current template")
        try:
            plan = open_execution_plan(dep.original_execution_plan_enc)
            ctx_json = decrypt(dep.original_context_enc, strict=True)
            ctx = json.loads(ctx_json)
            if (not isinstance(ctx, dict) or not isinstance(ctx.get("src_url"), str)
                    or not ctx["src_url"] or any(not isinstance(ctx.get(k), int) or isinstance(ctx.get(k), bool)
                                               or ctx[k] < 1 for k in ("cpu", "ram", "disk"))
                    or plan["owner_id"] != dep.owner_id):
                raise ValueError("invalid snapshot")
            recipe, blocks = materialize_execution_plan(plan)
        except (ValueError, TypeError, KeyError):
            raise HTTPException(409, "original plan cannot be opened — secret key mismatch or corrupt snapshot")
        # Immutable definitions, live trust policy: demoting an author still revokes
        # permission to execute their custom Ansible on the shared controller.
        api._assert_trusted_ansible_blocks(session, recipe, blocks)
        return dep.original_execution_plan_enc, ctx_json, json.dumps(plan["deploy_inputs"])
    if mode != "current":
        raise HTTPException(400, "rebuild mode must be original or current")
    tpl = _visible_template(session, dep.template_id, user)
    base = session.get(Image, tpl.base_image_id) if tpl.base_image_id else None
    if not base or base.kind != "base":
        raise HTTPException(400, "template has no base image — edit it first")
    api._assert_trusted_ansible_blocks(session, load_recipe(tpl.recipe_json))
    answers = api._validate_deploy_inputs(session, tpl, supplied or {})
    plan_enc = api._build_admitted_execution_plan(session, tpl, dep.owner_id, answers)
    net = session.get(Network, dep.network_id) if dep.network_id else None
    if dep.network_id and (not net or net.connection_id != dep.connection_id):
        raise HTTPException(409, "deployment network is unavailable or belongs to a different connection")
    if read_only:
        ctx = json.dumps({"src_url": base.source_url, "checksum": base.checksum or "",
                          "checksum_algorithm": api._checksum_algo(base.checksum or ""),
                          "base_image_id": base.id,
                          "cpu": dep.cpu, "ram": dep.ram, "disk": dep.disk,
                          **read_only_network_ctx(session, net, dep.id)})
    else:
        ctx = api._build_job_ctx(session, base, dep.cpu, dep.ram, dep.disk, net, dep.id)
    return plan_enc, ctx, answers


def _revision_summary(plan: dict) -> dict:
    """Only identity and counts; never return values from accepted deployment answers."""
    raw = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return {"revision": hashlib.sha256(raw.encode()).hexdigest()[:16],
            "version": plan["version"],
            "blockCount": sum(len(sec.get("blocks", [])) for sec in plan["recipe"])}


def _differences(original: Optional[dict], current: dict, original_ctx=None, current_ctx=None):
    if original is None:
        return ["Original plan is unavailable; this is a new admission of the current template."]
    changes = []
    # Values (including secrets) stay inside the encrypted snapshots. Summaries
    # deliberately disclose only the affected category, not a textual JSON diff.
    if original["recipe"] != current["recipe"]:
        changes.append("Recipe placements, inputs or answers changed.")
    if original["blocks"] != current["blocks"]:
        changes.append("Block definitions or input schemas changed.")
    if original_ctx is not None and current_ctx is not None:
        if any(original_ctx.get(k) != current_ctx.get(k) for k in ("src_url", "checksum", "checksum_algorithm")):
            changes.append("Base image source or checksum changed.")
        if any(original_ctx.get(k) != current_ctx.get(k) for k in ("cpu", "ram", "disk")):
            changes.append("CPU, memory or disk settings changed.")
        if any(original_ctx.get(k) != current_ctx.get(k) for k in ("network_mode", "bridge", "vlan", "dns", "ipconfig0")):
            changes.append("Network settings changed.")
    return changes


@router.get("/deployments/{dep_id}/rebuild-plan")
def rebuild_plan(dep_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    from . import api
    dep = api._owned_deployment(session, dep_id, user)
    original = None
    if dep.original_execution_plan_enc and dep.original_context_enc:
        try:
            original = open_execution_plan(dep.original_execution_plan_enc)
        except ValueError:
            pass
    current_id = None
    try:
        current_id = _visible_template(session, dep.template_id, user).id
    except HTTPException:
        pass
    return {"originalAvailable": original is not None,
            "original": _revision_summary(original) if original else None,
            "currentTemplateId": current_id}


@router.post("/deployments/{dep_id}/rebuild-preview")
def rebuild_preview(dep_id: int, body: RebuildBody, user: User = Depends(current_user),
                    session: Session = Depends(get_session)):
    from . import api
    dep = api._owned_deployment(session, dep_id, user)
    try:
        enc, ctx, _ = prepare_rebuild(session, dep, user, body.mode, body.deployInputs, read_only=True)
        plan = open_execution_plan(enc)
        original = original_ctx = None
        if dep.original_execution_plan_enc and dep.original_context_enc:
            try:
                original = open_execution_plan(dep.original_execution_plan_enc)
                original_ctx = json.loads(decrypt(dep.original_context_enc, strict=True))
            except (TypeError, ValueError):
                pass
        return {"mode": body.mode, "plan": _revision_summary(plan),
                "changes": _differences(original, plan, original_ctx, json.loads(ctx))}
    finally:
        session.rollback()


class BundlePlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=100)
    placementId: Optional[str] = None
    name: str = ""
    inputs: dict = Field(default_factory=dict)
    ask: list[str] = Field(default_factory=list)


class BundleSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = ""
    name: str = ""
    blocks: list[BundlePlacement] = Field(default_factory=list, max_length=500)


class BundleTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    os_family: str = "ubuntu"
    recipe: list[BundleSection] = Field(default_factory=list, max_length=100)
    cpu: int = Field(default=1, ge=1, le=256)
    ram: int = Field(default=2, ge=1, le=1024)
    disk: int = Field(default=20, ge=1, le=16384)


class BundleBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)
    name: str
    description: str = ""
    category: str = "Custom"
    icon: str = "box"
    section: str = "Scripts"
    phase: Literal["ansible", "cloudinit"]
    input_schema: list = Field(default_factory=list)
    ansible_template: str = ""
    cloudinit_template: str = ""


class BundleImage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    os_family: str
    source_url: str
    checksum: str


class TemplateBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["goblindock-template"]
    version: Literal[1]
    template: BundleTemplate
    customBlocks: list[BundleBlock] = Field(default_factory=list, max_length=500)
    baseImage: Optional[BundleImage] = None
    secretReferences: list[str] = Field(default_factory=list)


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle: dict
    connectionId: int = Field(gt=0)
    networkId: int = Field(gt=0)
    baseImageId: int = Field(gt=0)
    name: Optional[str] = None


def _portable_url(url):
    """Avoid exporting URL credentials or signed query parameters as image metadata."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parts.port:
            host += ":" + str(parts.port)
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return ""


def _portable_schema(block):
    try:
        schema = normalize_input_schema(json.loads(block.input_schema_json or "[]"))
    except (TypeError, ValueError):
        raise HTTPException(409, "block has invalid input schema")
    if input_schema_problems(schema, require_type=True):
        raise HTTPException(409, "block has invalid input schema")
    for field in schema:
        if field.get("type") in ("password", "secret") and not is_deployer_secret_ref(field.get("default")):
            field.pop("default", None)
    return schema


@router.get("/templates/{template_id}/export")
def export_template(template_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    tpl = _visible_template(session, template_id, user)
    # Source code of private custom blocks may be referenced by public templates;
    # exporting it follows the same ownership rule as editing/forking those blocks.
    if tpl.owner_id != user.id and user.role != "admin":
        raise HTTPException(404, "template not found")
    try:
        recipe = ensure_placement_ids(load_recipe(tpl.recipe_json))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    refs = {p["ref"] for sec in recipe for p in sec.get("blocks", [])}
    blocks = {b.key: b for b in session.exec(select(Block).where(Block.key.in_(refs))).all()} if refs else {}
    if set(blocks) != refs:
        raise HTTPException(409, "template references an unavailable block")
    custom = []
    for key, block in blocks.items():
        schema = _portable_schema(block)
        if not block.builtin:
            if block.owner_id != user.id and user.role != "admin":
                raise HTTPException(403, "template references a block you cannot export")
            custom.append({"key": key, **{k: getattr(block, k) for k in (
                "name", "description", "category", "icon", "section", "phase",
                "ansible_template", "cloudinit_template")}, "input_schema": schema})
        sensitive = {f["name"] for f in schema if f.get("type") in ("password", "secret")}
        for sec in recipe:
            for placed in sec.get("blocks", []):
                if placed["ref"] != key:
                    continue
                inputs = placed.get("inputs") or {}
                # Unknown legacy extension inputs are not portable configuration.
                known_names = {field["name"] for field in schema}
                inputs = {name: value for name, value in inputs.items() if name in known_names}
                for name in sensitive:
                    if name in inputs and not is_deployer_secret_ref(inputs[name]):
                        inputs.pop(name)
                        placed["ask"] = list(dict.fromkeys([*(placed.get("ask") or []), name]))
                placed["inputs"] = inputs
    # Serialize only known fields; no deployment rows, encrypted credentials,
    # target settings, source ownership or arbitrary recipe extension properties.
    recipe = [{"id": sec.get("id", ""), "name": sec.get("name", ""), "blocks": [
        {k: p[k] for k in ("ref", "name", "placementId", "inputs", "ask") if k in p}
        for p in sec.get("blocks", [])]} for sec in recipe]
    base = session.get(Image, tpl.base_image_id) if tpl.base_image_id else None
    result = {"format": "goblindock-template", "version": 1,
              "template": {"name": tpl.name, "description": tpl.description,
                           "os_family": tpl.os_family, "recipe": recipe,
                           "cpu": tpl.default_cpu, "ram": tpl.default_ram, "disk": tpl.default_disk},
              "customBlocks": custom,
              "baseImage": {"name": base.name, "os_family": base.os_family,
                            "source_url": _portable_url(base.source_url), "checksum": base.checksum} if base else None}
    result["secretReferences"] = sorted(set(re.findall(r"\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}", json.dumps(result))))
    return result


@router.post("/templates/import")
def import_template(body: ImportBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    from . import api
    if len(json.dumps(body.bundle)) > 2_000_000:
        raise HTTPException(400, "template bundle exceeds 2 MB")
    try:
        bundle = TemplateBundle.model_validate(body.bundle)
        recipe = ensure_placement_ids([section.model_dump(exclude_none=True) for section in bundle.template.recipe])
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(400, "invalid template bundle schema or placement IDs")
    payload = api.TemplateBody(**bundle.template.model_dump(exclude={"recipe"}), recipe=recipe,
                               baseImageId=body.baseImageId, connectionId=body.connectionId,
                               networkId=body.networkId, public=False)
    bid, cid, nid = api._validate_template_refs(session, payload)
    custom_keys = [b.key for b in bundle.customBlocks]
    if len(custom_keys) != len(set(custom_keys)):
        raise HTTPException(400, "duplicate custom block keys")
    refs = {p.get("ref") for sec in recipe if isinstance(sec, dict)
            for p in sec.get("blocks", []) if isinstance(p, dict)}
    if not set(custom_keys).issubset(refs):
        raise HTTPException(400, "bundle contains unused custom blocks")
    builtins = {b.key for b in session.exec(select(Block).where(Block.builtin == True)).all()}
    if refs - set(custom_keys) - builtins:
        raise HTTPException(400, "bundle references unavailable built-in blocks")
    fresh = {}
    for block in bundle.customBlocks:
        if block.phase == "ansible" and user.role != "admin":
            raise HTTPException(403, api._CUSTOM_ANSIBLE_ADMIN_ONLY)
        problems = lint_block(block.phase, block.input_schema, block.ansible_template, block.cloudinit_template)
        if problems:
            raise HTTPException(400, "Block validation failed: " + "; ".join(problems))
        schema = normalize_input_schema(block.input_schema)
        for field in schema:
            if (field.get("type") in ("password", "secret") and field.get("default") not in (None, "")
                    and not is_deployer_secret_ref(field["default"])):
                raise HTTPException(400, "bundle contains a literal sensitive default")
        fresh[block.key] = Block(key=api._new_block_key(session), kind="custom", builtin=False,
                                owner_id=user.id, input_schema_json=json.dumps(schema),
                                **block.model_dump(exclude={"key", "input_schema"}))
    # A nested transaction rolls back all imported blocks if any recipe validation
    # fails. Imports cannot leave abandoned custom code behind after a 4xx.
    with session.begin_nested():
        for block in fresh.values():
            session.add(block)
        session.flush()
        for sec in recipe:
            for placed in sec.get("blocks", []):
                if placed["ref"] in fresh:
                    placed["ref"] = fresh[placed["ref"]].key
        api._validate_recipe(session, recipe, user)
        api._validate_recipe_sensitive_inputs(session, recipe)
        tpl = Template(name=(body.name or payload.name).strip() or "Imported template",
                       description=payload.description, os_family=payload.os_family,
                       recipe_json=json.dumps(recipe), default_cpu=payload.cpu,
                       default_ram=payload.ram, default_disk=payload.disk,
                       owner_id=user.id, public=False, base_image_id=bid,
                       connection_id=cid, network_id=nid)
        session.add(tpl); session.flush()
        api.record_audit(session, user, "template.import", "template", tpl.id, tpl.name)
    session.commit()
    statebus.bump()
    return {"ok": True, "templateId": tpl.id, "customBlockCount": len(fresh),
            "secretReferences": bundle.secretReferences}

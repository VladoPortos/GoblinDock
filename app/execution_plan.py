"""Immutable, encrypted execution snapshots for deployment jobs.

The admission request captures the accepted recipe, deploy-time answers, and every
referenced block.  Workers then execute this snapshot instead of mutable template
or block rows.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlmodel import Session, select

from .models import Block, Template
from .recipes import (
    input_schema_problems,
    load_recipe,
    merge_deploy_inputs,
    normalize_input_schema,
)
from .security import decrypt, encrypt


_BLOCK_FIELDS = (
    "key", "kind", "name", "description", "category", "icon", "section", "phase",
    "input_schema_json", "ansible_template", "cloudinit_template", "owner_id", "builtin",
)
_LEGACY_PLAN_FIELDS = {"version", "owner_id", "recipe", "blocks", "deploy_inputs"}
_PLAN_FIELDS = _LEGACY_PLAN_FIELDS | {
    "sensitive_fields", "template_owner_id", "deployment_owner_id",
}


def _invalid() -> None:
    raise ValueError("invalid execution plan")


def _is_owner_id(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        _invalid()


def _recipe_block_refs(recipe: list) -> set[str]:
    refs: set[str] = set()
    for section in recipe:
        if not isinstance(section, dict):
            _invalid()
        placements = section.get("blocks", [])
        if not isinstance(placements, list):
            _invalid()
        for placement in placements:
            if not isinstance(placement, dict):
                _invalid()
            ref = placement.get("ref")
            if not isinstance(ref, str) or not ref:
                _invalid()
            if "inputs" in placement and not isinstance(placement["inputs"], dict):
                _invalid()
            refs.add(ref)
    return refs


def _validate_plan(plan: object) -> dict:
    if not isinstance(plan, dict) or set(plan) not in (_LEGACY_PLAN_FIELDS, _PLAN_FIELDS):
        _invalid()
    if plan["version"] != 1 or isinstance(plan["version"], bool):
        _invalid()
    if not _is_owner_id(plan["owner_id"]):
        _invalid()
    current = set(plan) == _PLAN_FIELDS
    if current:
        if (not _is_owner_id(plan["template_owner_id"])
                or not _is_owner_id(plan["deployment_owner_id"])
                or plan["owner_id"] != plan["deployment_owner_id"]):
            _invalid()
    recipe = plan["recipe"]
    blocks = plan["blocks"]
    deploy_inputs = plan["deploy_inputs"]
    if not isinstance(recipe, list) or not isinstance(blocks, dict) or not isinstance(deploy_inputs, dict):
        _invalid()
    refs = _recipe_block_refs(recipe)
    if set(blocks) != refs:
        _invalid()
    derived_sensitive: dict[str, list[str]] = {}
    for key, snapshot in blocks.items():
        if not isinstance(key, str) or not key or not isinstance(snapshot, dict):
            _invalid()
        if set(snapshot) != set(_BLOCK_FIELDS) or snapshot.get("key") != key:
            _invalid()
        if not isinstance(snapshot["input_schema_json"], str):
            _invalid()
        try:
            schema = json.loads(snapshot["input_schema_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            _invalid()
        if input_schema_problems(schema, require_type=True):
            _invalid()
        derived_sensitive[key] = sorted({
            field.get("name") for field in schema
            if isinstance(field.get("name"), str)
            and field.get("type") in ("password", "secret")
        })
        if not isinstance(snapshot["ansible_template"], str) or not isinstance(snapshot["cloudinit_template"], str):
            _invalid()
        if not _is_owner_id(snapshot["owner_id"]):
            _invalid()
        if not isinstance(snapshot["builtin"], bool):
            _invalid()
        for field in ("kind", "name", "description", "category", "icon", "section", "phase"):
            if not isinstance(snapshot[field], str):
                _invalid()
    if current and plan["sensitive_fields"] != derived_sensitive:
        _invalid()
    return plan


def build_execution_plan(session: Session, template: Template, deployment_owner_id: Optional[int],
                         deploy_inputs_json: str) -> dict:
    """Capture the accepted recipe and its block definitions for one job."""
    if not _is_owner_id(deployment_owner_id):
        _invalid()
    try:
        deploy_inputs = json.loads(deploy_inputs_json or "{}")
    except (json.JSONDecodeError, TypeError):
        _invalid()
    if not isinstance(deploy_inputs, dict):
        _invalid()
    recipe = load_recipe(template.recipe_json)
    recipe = merge_deploy_inputs(recipe, deploy_inputs)
    refs = _recipe_block_refs(recipe)
    rows = session.exec(select(Block).where(Block.key.in_(refs))).all() if refs else []
    if {block.key for block in rows} != refs:
        _invalid()
    blocks = {
        block.key: {field: getattr(block, field) for field in _BLOCK_FIELDS}
        for block in rows
    }
    sensitive_fields = {}
    for key, snapshot in blocks.items():
        schema = normalize_input_schema(
            json.loads(snapshot["input_schema_json"] or "[]"),
        )
        snapshot["input_schema_json"] = json.dumps(schema)
        sensitive_fields[key] = sorted({
            field.get("name") for field in schema
            if isinstance(field, dict)
            and isinstance(field.get("name"), str)
            and field.get("type") in ("password", "secret")
        })
    return _validate_plan({
        "version": 1,
        "owner_id": deployment_owner_id,
        "template_owner_id": template.owner_id,
        "deployment_owner_id": deployment_owner_id,
        "recipe": recipe,
        "blocks": blocks,
        "sensitive_fields": sensitive_fields,
        "deploy_inputs": deploy_inputs,
    })


def encrypt_deploy_inputs(deploy_inputs_json: str) -> str:
    """Encrypt a deployment's ask-on-deploy answers for at-rest storage. The
    answers may hold literal credentials typed at deploy time, so they never
    land in the database as plaintext."""
    return encrypt(deploy_inputs_json or "{}")


def open_deploy_inputs(ciphertext: str) -> str:
    """Decrypt stored ask-on-deploy answers. Fails closed (raises ValueError) on
    a rotated key / corrupt ciphertext instead of silently rebuilding without
    the deployer's answers; an empty column is a legitimate 'no answers'."""
    if not ciphertext:
        return "{}"
    return decrypt(ciphertext, strict=True) or "{}"


def seal_execution_plan(plan: dict) -> str:
    """Validate, canonicalize, then encrypt an execution plan."""
    return encrypt(_canonical_json(_validate_plan(plan)))


def open_execution_plan(ciphertext: str) -> dict:
    """Decrypt and strictly validate an execution plan, failing closed."""
    try:
        plaintext = decrypt(ciphertext, strict=True)
        value = json.loads(plaintext)
        return _validate_plan(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        _invalid()


def materialize_execution_plan(plan: dict) -> tuple[list[dict], dict[str, Block]]:
    """Return detached recipe and block objects suitable for recipe compilation."""
    valid = _validate_plan(plan)
    recipe = json.loads(_canonical_json(valid["recipe"]))
    blocks = {key: Block(**snapshot) for key, snapshot in valid["blocks"].items()}
    return recipe, blocks

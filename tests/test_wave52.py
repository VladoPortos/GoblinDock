"""Wave 52 — twelve new builtin blocks (networking, monitoring, git, shares).

WireGuard, Caddy, Nginx site, DNS, Chrony · Node Exporter, Netdata, Promtail ·
Import SSH Keys (GitHub/GitLab), Git Client + Token · NFS Export, Samba Share.
Also pins catalogue-wide invariants every future block must keep: clean lint,
sensitive defaults that are real deployer secret references (or blank), and
source templates free of deployer-scoped references (cross-owner deployable).

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave52.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave52-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

import yaml  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.models import Block  # noqa: E402
from app.recipes import (  # noqa: E402
    _REF_RE,
    compile_playbook,
    is_deployer_secret_ref,
    lint_block,
)
from app.seed import seed_blocks  # noqa: E402

init_db()
seed_blocks()

NEW_BLOCKS = {
    "b-wireguard": "Networking",
    "b-caddy": "Networking",
    "b-nginxsite": "Networking",
    "b-dns": "Networking",
    "b-chrony": "Networking",
    "b-nodeexporter": "Monitoring",
    "b-netdata": "Monitoring",
    "b-promtail": "Monitoring",
    "b-importsshkeys": "Users / SSH",
    "b-gitclient": "Files",
    "b-nfsexport": "Files",
    "b-sambashare": "Files",
}


def _all_blocks() -> dict:
    with session_scope() as s:
        return {b.key: Block(**b.model_dump()) for b in s.exec(select(Block)).all()}


def _sample_inputs(block: Block) -> dict:
    inputs = {}
    for field in json.loads(block.input_schema_json or "[]"):
        ftype = field.get("type", "text")
        if ftype == "bool":
            inputs[field["name"]] = bool(field.get("default", False))
        elif ftype == "select":
            inputs[field["name"]] = field.get("default") or field["options"][0]
        elif ftype in ("secret", "password"):
            inputs[field["name"]] = "sample-sensitive-value"
        else:
            inputs[field["name"]] = field.get("default") or f"sample-{field['name']}"
    return inputs


def test_new_blocks_are_seeded_with_expected_shape():
    blocks = _all_blocks()
    for key, category in NEW_BLOCKS.items():
        block = blocks.get(key)
        assert block is not None, f"{key} missing after seed"
        assert block.builtin is True and block.kind == "builtin"
        assert block.category == category, (key, block.category)
        assert block.phase == "ansible", key
        assert block.ansible_template.strip(), key


def test_every_builtin_block_lints_clean():
    blocks = _all_blocks()
    for key, block in sorted(blocks.items()):
        if not block.builtin:
            continue
        problems = lint_block(block.phase, block.input_schema_json,
                              block.ansible_template, block.cloudinit_template)
        assert not problems, (key, problems)


def test_builtin_sensitive_defaults_are_references_or_blank():
    """A literal credential in a builtin schema default would land in plaintext
    in every recipe placing the block — defaults must be blank or a full
    deployer secret reference."""
    for key, block in sorted(_all_blocks().items()):
        if not block.builtin:
            continue
        for field in json.loads(block.input_schema_json or "[]"):
            if field.get("type") not in ("password", "secret"):
                continue
            default = field.get("default") or ""
            assert default == "" or is_deployer_secret_ref(default), (
                key, field.get("name"), default)


def test_builtin_sources_carry_no_deployer_scoped_references():
    """Cross-owner admission rejects any {{ secrets.* }}/{{ variable.* }} in
    block source — a builtin carrying one would break every public template."""
    for key, block in sorted(_all_blocks().items()):
        if not block.builtin:
            continue
        for source in (block.ansible_template, block.cloudinit_template):
            match = _REF_RE.search(source or "")
            assert match is None, (key, match.group(0))


def test_new_blocks_compose_valid_playbooks_with_placeholders_resolved():
    blocks = _all_blocks()
    for key in NEW_BLOCKS:
        block = blocks[key]
        inputs = _sample_inputs(block)
        recipe = [{"blocks": [{"ref": key, "inputs": inputs}]}]
        playbook = compile_playbook(recipe, {key: block}, template_name=key)
        parsed = yaml.safe_load(playbook)
        assert isinstance(parsed, list) and parsed[0]["tasks"], key
        for name, value in inputs.items():
            assert "{" + name + "}" not in playbook, (key, name)
        # a scalar sample value must actually reach the rendered playbook
        probe = next((v for v in inputs.values() if isinstance(v, str) and v), None)
        if probe:
            assert probe in playbook, (key, probe)


def test_reseed_is_idempotent_for_new_blocks():
    seed_blocks()
    with session_scope() as s:
        for key in NEW_BLOCKS:
            rows = s.exec(select(Block).where(Block.key == key)).all()
            assert len(rows) == 1, (key, len(rows))


if __name__ == "__main__":
    test_new_blocks_are_seeded_with_expected_shape()
    test_every_builtin_block_lints_clean()
    test_builtin_sensitive_defaults_are_references_or_blank()
    test_builtin_sources_carry_no_deployer_scoped_references()
    test_new_blocks_compose_valid_playbooks_with_placeholders_resolved()
    test_reseed_is_idempotent_for_new_blocks()
    print("\nALL WAVE 52 UNIT TESTS PASSED")

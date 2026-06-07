"""Compile a block recipe into (a) a readable Ansible playbook for the YAML
viewer and (b) cloud-init runcmd shell lines used to actually bake a golden
image / configure a deploy.

Templates are stored on each Block as strings with ``{key}`` placeholders that
are filled from the placed block's inputs (plus ``{{ secrets.NAME }}`` refs which
are resolved against the secrets store at run time). We keep the renderer
deliberately small and predictable rather than a full Jinja engine.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Callable, Optional

import yaml

from .models import Block

# {{ secrets.NAME }} (encrypted, masked) and {{ variable.NAME }} (plaintext, visible)
_REF_RE = re.compile(r"\{\{\s*(secrets|variable)\.([A-Za-z0-9_]+)\s*\}\}")


_PLACEHOLDER_RE = re.compile(r"(?P<indent>[^\S\n]*)\{(?P<key>[A-Za-z0-9_]+)\}")


class _Default(dict):
    """str.format_map helper: an unknown {placeholder} collapses to '' instead of
    raising KeyError, so render_shell can reference an optional input safely.
    (Without this, render_shell's format_map raised NameError on every call and the
    bare except returned the template UNRENDERED — cloud-init blocks never substituted
    their inputs. Defining it makes render_shell actually render + shell-quote.)"""
    def __missing__(self, key):  # noqa: D401
        return ""


def _substitute(template: str, flat: dict) -> str:
    """Indentation-aware placeholder fill: when a placeholder is filled with a
    multi-line value (e.g. a Run Script body under a YAML `|` block scalar),
    re-indent the continuation lines to the placeholder's column so the generated
    playbook stays valid YAML. Unknown placeholders collapse to ''."""
    def _sub(m: "re.Match") -> str:
        indent = m.group("indent")
        key = m.group("key")
        if key not in flat:
            return indent
        val = str(flat[key])
        if "\n" in val:
            return indent + ("\n" + indent).join(val.split("\n"))
        return indent + val

    return _PLACEHOLDER_RE.sub(_sub, template)


def resolve_secrets(text: str, lookup: Callable[[str, str], str]) -> str:
    """Resolve {{ secrets.NAME }} and {{ variable.NAME }} via lookup(namespace, name)."""
    def _sub(m):
        val = lookup(m.group(1), m.group(2))
        return val if val else m.group(0)
    return _REF_RE.sub(_sub, text)


def _schema_types(block: Block) -> dict:
    try:
        schema = json.loads(block.input_schema_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return {}
    return {f["name"]: f.get("type", "text") for f in schema if isinstance(f, dict) and "name" in f}


def render_shell(template: str, inputs: dict, types: dict, secret_lookup: Callable[[str], str]) -> str:
    """Render a block's cloud-init template treating inputs as DATA: every value
    is secret-resolved then shell-quoted, EXCEPT 'code' fields (the Run Script
    body, which is intentionally arbitrary shell on the user's own VM)."""
    if not template:
        return ""
    flat: dict = {}
    for k, v in (inputs or {}).items():
        t = types.get(k, "text")
        if isinstance(v, list):
            items = [resolve_secrets(str(x), secret_lookup) for x in v]
            flat[k] = " ".join(items) if t == "code" else " ".join(shlex.quote(x) for x in items)
        elif isinstance(v, bool):
            flat[k] = "true" if v else "false"
        else:
            sval = resolve_secrets(str(v), secret_lookup)
            flat[k] = sval if t == "code" else shlex.quote(sval)
    try:
        return template.format_map(_Default(flat))
    except Exception:  # noqa: BLE001
        return template


def _ansible_flat(inputs: dict, types: dict,
                  secret_lookup: Optional[Callable[[str, str], str]]) -> dict:
    """Substitution dict for an ansible task template. Like render_shell (the cloud-init
    path), every value is secret-resolved (when a lookup is given) and exposed in forms
    that let a template place DATA safely instead of splicing raw text:
      {k}        raw — for ansible MODULE args, where ansible itself quotes
      {k_q}      shell-quoted via shlex — for a value inside an ansible.builtin.shell cmd
      {k_yamlq}  JSON-encoded — a safe double-quoted YAML scalar
    'code'-typed fields stay raw in {k_q} (a Run Script body is intentionally shell)."""
    def _res(x) -> str:
        return resolve_secrets(str(x), secret_lookup) if secret_lookup else str(x)
    flat: dict = {}
    for k, v in (inputs or {}).items():
        t = types.get(k, "text")
        if isinstance(v, list):
            items = [_res(x) for x in v]
            flat[k] = " ".join(items)
            flat[f"{k}_yaml"] = "[" + ", ".join(items) + "]"
            flat[f"{k}_yamlq"] = "[" + ", ".join(json.dumps(x) for x in items) + "]"
            flat[f"{k}_q"] = " ".join(shlex.quote(x) for x in items)
        elif isinstance(v, bool):
            flat[k] = flat[f"{k}_q"] = flat[f"{k}_yamlq"] = "true" if v else "false"
        else:
            sval = _res(v)
            flat[k] = sval
            flat[f"{k}_q"] = sval if t == "code" else shlex.quote(sval)
            flat[f"{k}_yamlq"] = json.dumps(sval)
    return flat


def mask_secrets(text: str) -> str:
    return _REF_RE.sub(lambda m: f"<{'secret' if m.group(1) == 'secrets' else 'variable'} {m.group(2)}>", text)


def load_recipe(recipe_json: str) -> list[dict]:
    try:
        data = json.loads(recipe_json or "[]")
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def ask_map(recipe: list[dict]) -> dict[str, list[str]]:
    """Ask-on-deploy index: ``{"<sectionIdx>.<blockIdx>": [input names]}`` for
    every placed block carrying a non-empty ``ask`` list."""
    out: dict[str, list[str]] = {}
    for si, sec in enumerate(recipe):
        if not isinstance(sec, dict):
            continue
        for bi, block in enumerate(sec.get("blocks") or []):
            if not isinstance(block, dict):
                continue
            asks = [a for a in (block.get("ask") or []) if isinstance(a, str)]
            if asks:
                out[f"{si}.{bi}"] = asks
    return out


def merge_deploy_inputs(recipe: list[dict], overrides: dict) -> list[dict]:
    """Overlay deploy-time answers onto a template's recipe. Only inputs listed
    in the addressed block's own ``ask`` array are applied; unknown addresses,
    names or shapes are silently ignored (defense in depth — the API already
    validated them). Returns a deep copy; never mutates the stored recipe."""
    out = json.loads(json.dumps(recipe))  # deep copy — never hand back the stored object
    if not overrides or not isinstance(overrides, dict):
        return out
    allowed = ask_map(recipe)
    for addr, answers in overrides.items():
        names = allowed.get(addr)
        if not names or not isinstance(answers, dict):
            continue
        try:
            si, bi = (int(x) for x in addr.split("."))
        except (ValueError, TypeError):
            continue
        block = out[si]["blocks"][bi]
        if not isinstance(block, dict):
            continue
        inputs = block.get("inputs") or {}
        for name, value in answers.items():
            if name in names:
                inputs[name] = value
        block["inputs"] = inputs
    return out


def _schema_defaults(block: Block) -> dict:
    try:
        schema = json.loads(block.input_schema_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return {}
    return {f["name"]: f.get("default") for f in schema if isinstance(f, dict) and "name" in f}


def _merged_inputs(block: Block, placed: dict) -> dict:
    """Block schema defaults, overlaid with whatever the user filled in."""
    merged = _schema_defaults(block)
    user_inputs = placed.get("inputs") or {}
    for k, v in user_inputs.items():
        if v not in (None, ""):
            merged[k] = v
    return merged


def _ansible_playbook(recipe: list[dict], blocks_by_key: dict[str, Block],
                      name: str, secret_lookup: Optional[Callable[[str], str]] = None) -> str:
    """Build an Ansible playbook from the phase='ansible' blocks (post-boot)."""
    # Defense-in-depth at the SINK: even though create/patch validate names, a
    # stored/legacy/preview name could carry a newline that would inject sibling YAML
    # keys here — strip control chars so the name stays a single scalar.
    name = re.sub(r"[\x00-\x1f\x7f]", " ", name or "goblindock").strip() or "goblindock"
    lines = [
        "---",
        f"# generated by GoblinDock · template: {name}",
        f"- name: {name}",
        "  hosts: all",
        "  become: true",
        "  gather_facts: false",
        "  tasks:",
    ]
    any_task = False
    for section in recipe:
        for placed in section.get("blocks", []):
            block = blocks_by_key.get(placed.get("ref", ""))
            if not block or block.phase != "ansible" or not block.ansible_template:
                continue
            flat = _ansible_flat(_merged_inputs(block, placed), _schema_types(block), secret_lookup)
            rendered = _substitute(block.ansible_template, flat)
            # belt-and-braces: resolve/mask any secret ref written directly in a template
            rendered = (resolve_secrets(rendered, secret_lookup) if secret_lookup
                        else mask_secrets(rendered))
            for ln in rendered.splitlines():
                lines.append("    " + ln if ln.strip() else ln)
            lines.append("")
            any_task = True
    if not any_task:
        lines.append("    - name: nothing to do (no post-boot blocks)")
        lines.append("      ansible.builtin.debug: { msg: 'ok' }")
    return "\n".join(lines).rstrip() + "\n"


def compile_ansible(recipe: list[dict], blocks_by_key: dict[str, Block],
                    secret_lookup: Callable[[str], str], name: str = "goblindock") -> str:
    """Runnable Ansible playbook (secrets resolved) for ansible-phase blocks."""
    return _ansible_playbook(recipe, blocks_by_key, name, secret_lookup)


def has_ansible_blocks(recipe: list[dict], blocks_by_key: dict[str, Block]) -> bool:
    for section in recipe:
        for placed in section.get("blocks", []):
            b = blocks_by_key.get(placed.get("ref", ""))
            if b and b.phase == "ansible" and b.ansible_template:
                return True
    return False


def compile_playbook(recipe: list[dict], blocks_by_key: dict[str, Block], image_name: str = "recipe") -> str:
    """Read-only preview: the Ansible playbook (post-boot) + a comment listing the
    cloud-init (first-boot) steps. Secrets are masked."""
    pb = _ansible_playbook(recipe, blocks_by_key, image_name)
    ci = []
    for section in recipe:
        for placed in section.get("blocks", []):
            b = blocks_by_key.get(placed.get("ref", ""))
            if b and b.phase == "cloudinit" and b.cloudinit_template:
                ci.append(f"#   - {b.name}")
    if ci:
        pb += "\n# --- cloud-init (first-boot) steps in this template ---\n" + "\n".join(ci) + "\n"
    return pb


def compile_cloudinit(
    recipe: list[dict],
    blocks_by_key: dict[str, Block],
    secret_lookup: Callable[[str], str],
) -> list[str]:
    """Shell command lines for cloud-init runcmd — only phase='cloudinit' blocks
    (run as root at first boot). Inputs/secrets are shell-quoted (injection-safe)."""
    cmds: list[str] = ["set -e"]
    for section in recipe:
        for placed in section.get("blocks", []):
            block = blocks_by_key.get(placed.get("ref", ""))
            if not block or block.phase != "cloudinit" or not block.cloudinit_template:
                continue
            rendered = render_shell(
                block.cloudinit_template, _merged_inputs(block, placed),
                _schema_types(block), secret_lookup,
            )
            cmds.append("echo " + shlex.quote(f">>> GoblinDock: {block.name}"))
            for ln in rendered.splitlines():
                if ln.strip():
                    cmds.append(ln)
    return cmds


# --------------------------------------------------------------------------- #
# block linting / dry-run validation                                           #
# --------------------------------------------------------------------------- #
# Allowed values for a custom block input field's `type`. Kept liberal — the UI
# only emits a few of these, but we tolerate synonyms rather than reject a usable
# block on a cosmetic type label.
_ALLOWED_INPUT_TYPES = {
    "text", "code", "number", "password", "secret",
    "select", "list", "bool", "boolean", "toggle",
}
_INPUT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _lint_sample(field: dict):
    """A representative sample value for a schema field, used only to render the
    template during a dry-run (never executed)."""
    t = field.get("type") or "text"
    default = field.get("default")
    if default not in (None, ""):
        return default
    if t == "list":
        return ["sample"]
    if t in ("bool", "boolean", "toggle"):
        return True
    if t == "number":
        return 1
    return "sample"


def _yaml_err(e: Exception) -> str:
    return " ".join(str(e).split())[:200]


def lint_block(phase: str, input_schema, ansible_template: str,
               cloudinit_template: str) -> list[str]:
    """Validate a custom block WITHOUT executing anything. Returns a list of human
    problems (empty = clean). The renderer (`render_shell` / `_substitute`) is
    deliberately error-SWALLOWING, so this does its own strict checks and then
    `yaml.safe_load`s the COMPOSED ansible playbook (the real signal) — it validates
    the literal post-substitution YAML, not arbitrary runtime Jinja a block may carry.
    """
    problems: list[str] = []

    schema = input_schema
    if isinstance(schema, str):
        try:
            schema = json.loads(schema or "[]")
        except (json.JSONDecodeError, TypeError):
            return ["input schema is not valid JSON"]
    if not isinstance(schema, list):
        return ["input schema must be a list of fields"]

    seen: set[str] = set()
    for i, f in enumerate(schema):
        if not isinstance(f, dict):
            problems.append(f"input field #{i + 1} must be an object")
            continue
        name = f.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"input field #{i + 1} is missing a name")
            continue
        if not _INPUT_NAME_RE.fullmatch(name):
            problems.append(
                f"input name {name!r} must start with a letter/underscore and use "
                "only letters, digits or underscores")
        if name in seen:
            problems.append(f"duplicate input name {name!r}")
        seen.add(name)
        t = f.get("type")
        if t is not None and t not in _ALLOWED_INPUT_TYPES:
            problems.append(f"input {name!r} has unknown type {t!r}")

    phase = "cloudinit" if phase == "cloudinit" else "ansible"
    active_tmpl = cloudinit_template if phase == "cloudinit" else ansible_template
    if not (active_tmpl or "").strip():
        problems.append(f"a {phase} block needs a non-empty {phase} template")

    # A broken schema makes a render meaningless — surface the schema errors first.
    if problems:
        return problems

    sample = {f["name"]: _lint_sample(f) for f in schema
              if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"].strip()}
    block = Block(key="lint", name="lint", phase=phase,
                  input_schema_json=json.dumps(schema),
                  ansible_template=ansible_template or "",
                  cloudinit_template=cloudinit_template or "")

    if phase == "ansible":
        recipe = [{"blocks": [{"ref": "lint", "inputs": sample}]}]
        rendered = _ansible_playbook(recipe, {"lint": block}, "lint")  # secrets masked
        try:
            doc = yaml.safe_load(rendered)
        except yaml.YAMLError as e:
            problems.append(f"rendered Ansible is not valid YAML: {_yaml_err(e)}")
        else:
            tasks = (doc[0].get("tasks")
                     if isinstance(doc, list) and doc and isinstance(doc[0], dict) else None)
            if not isinstance(tasks, list) or not tasks:
                problems.append("rendered Ansible has no tasks — the task YAML must be a "
                                "list of Ansible tasks")
    else:
        rendered = render_shell(cloudinit_template or "", sample,
                                _schema_types(block), lambda ns, n: "")
        if not any(ln.strip() for ln in rendered.splitlines()):
            problems.append("cloud-init template renders to nothing")

    return problems


def recipe_block_chips(recipe: list[dict]) -> list[str]:
    chips: list[str] = []
    for section in recipe:
        for placed in section.get("blocks", []):
            chips.append(placed.get("name", placed.get("ref", "block")))
    return chips

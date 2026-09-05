"""Compile a block recipe into (a) a readable Ansible playbook for the YAML
viewer and (b) cloud-init runcmd shell lines used to configure a deploy.

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
_DEPLOYER_SECRET_REF_RE = re.compile(r"^\{\{\s*secrets\.[A-Za-z0-9_]+\s*\}\}$")


_PLACEHOLDER_RE = re.compile(r"(?P<indent>[^\S\n]*)\{(?P<key>[A-Za-z0-9_]+)\}")

# C0 control chars (incl. newline/tab) + DEL. A non-'code' input value's RAW form must
# never carry these — in a YAML scalar they can break the scalar and inject a sibling
# Ansible task (control-node RCE). See _ansible_flat's sink hardening.
_CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


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


def render_shell(template: str, inputs: dict, types: dict,
                 secret_lookup: Callable[[str, str], str]) -> str:
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
    # Use the regex substitutor (same as the ansible path) rather than str.format_map:
    # format_map treats every literal `{...}` in the template as a field reference, so
    # shell idioms like awk '{print $1}', ${HOME}, jq '{x:.y}' and brace-expansion
    # a.{txt,bak} were silently deleted or aborted rendering. _substitute only touches
    # {word} placeholders and leaves all other braces intact.
    return _substitute(template, flat)


def _ansible_flat(inputs: dict, types: dict,
                  secret_lookup: Optional[Callable[[str, str], str]]) -> dict:
    """Substitution dict for an ansible task template. Like render_shell (the cloud-init
    path), every value is secret-resolved (when a lookup is given) and exposed in forms
    that let a template place DATA safely instead of splicing raw text:
      {k}        raw — for ansible MODULE args, where ansible itself quotes
      {k_q}      shell-quoted via shlex — for a value inside an ansible.builtin.shell cmd
      {k_yamlq}  JSON-encoded — a safe double-quoted YAML scalar
      {k_set}    'true' if the scalar value is non-empty/non-false, 'false' otherwise —
                 safe for ansible ``when:`` clauses without embedding the value in YAML
    'code'-typed fields stay raw in {k_q} (a Run Script body is intentionally shell).

    SINK HARDENING: the raw ``{k}`` slot is spliced verbatim into the playbook YAML by
    several built-in blocks (e.g. ``name: {user}``, ``key: "{key}"``). A newline / control
    char in a non-'code' value there could break the scalar and inject a sibling task —
    e.g. one carrying ``delegate_to: localhost`` (code execution on the shared control
    node, not the tenant's VM). The API rejects such values on template save, but this
    also neutralises control chars in the raw form at compile time so a legacy row that
    predates that check can't inject either. 'code' bodies stay raw by design; the shell-
    quoted ({k_q}) and JSON-encoded ({k_yamlq}) forms already contain any control char."""
    def _res(x) -> str:
        return resolve_secrets(str(x), secret_lookup) if secret_lookup else str(x)

    def _raw(s: str, t: str) -> str:
        # Keep 'code' bodies verbatim (intentionally multi-line); scrub control chars
        # from every other value's raw form so it can never break out of a YAML scalar.
        return s if t == "code" else _CTRL_CHAR_RE.sub(" ", s)

    flat: dict = {}
    for k, v in (inputs or {}).items():
        t = types.get(k, "text")
        if isinstance(v, list):
            items = [_res(x) for x in v]
            raw_items = [_raw(x, t) for x in items]
            flat[k] = " ".join(raw_items)
            flat[f"{k}_yaml"] = "[" + ", ".join(raw_items) + "]"
            flat[f"{k}_yamlq"] = "[" + ", ".join(json.dumps(x) for x in items) + "]"
            flat[f"{k}_q"] = " ".join(shlex.quote(x) for x in items)
        elif isinstance(v, bool):
            flat[k] = flat[f"{k}_q"] = flat[f"{k}_yamlq"] = "true" if v else "false"
            flat[f"{k}_set"] = "true" if v else "false"
        else:
            sval = _res(v)
            flat[k] = _raw(sval, t)
            flat[f"{k}_q"] = sval if t == "code" else shlex.quote(sval)
            flat[f"{k}_yamlq"] = json.dumps(sval)
            flat[f"{k}_set"] = "true" if sval else "false"
    return flat


def mask_secrets(text: str) -> str:
    return _REF_RE.sub(lambda m: f"<{'secret' if m.group(1) == 'secrets' else 'variable'} {m.group(2)}>", text)


def is_deployer_secret_ref(value: object) -> bool:
    """Return true only for one complete ``{{ secrets.NAME }}`` reference."""
    return isinstance(value, str) and _DEPLOYER_SECRET_REF_RE.fullmatch(value) is not None


def load_recipe(recipe_json: str) -> list[dict]:
    try:
        data = json.loads(recipe_json or "[]")
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def validate_public_sensitive_inputs(
    recipe,
    schemas_by_ref,
    *,
    deploy_inputs=None,
    cross_owner=False,
    reject_unknown=False,
) -> None:
    """Reject author-supplied literals from public/cross-owner sensitive inputs.

    ``schemas_by_ref`` contains immutable schema lists keyed by block reference. During
    cross-owner admission, a sensitive ask-on-deploy field must have an answer at its
    exact placement address; the merged recipe value is then deployer-supplied and may
    be literal. A stored sensitive value may be blank only when that exact field is
    ask-on-deploy; otherwise it must be a deployer-scoped secret reference. Error
    messages intentionally contain block/field names only.
    """
    schemas_by_ref = schemas_by_ref if isinstance(schemas_by_ref, dict) else {}
    deploy_inputs = deploy_inputs if isinstance(deploy_inputs, dict) else {}
    if not isinstance(recipe, list):
        raise ValueError("recipe is unavailable")
    for si, section in enumerate(recipe):
        if not isinstance(section, dict):
            continue
        placements = section.get("blocks") or []
        if not isinstance(placements, list):
            continue
        for bi, placed in enumerate(placements):
            if not isinstance(placed, dict):
                continue
            ref = placed.get("ref")
            schema = schemas_by_ref.get(ref)
            if not isinstance(ref, str) or not isinstance(schema, list):
                if reject_unknown:
                    block_name = ref if isinstance(ref, str) and ref else f"{si}.{bi}"
                    raise ValueError(f"block {block_name!r} is unavailable")
                continue
            sensitive = {
                field.get("name") for field in schema
                if isinstance(field, dict)
                and isinstance(field.get("name"), str)
                and field.get("type") in ("password", "secret")
            }
            if not sensitive:
                continue
            inputs = placed.get("inputs") or {}
            if not isinstance(inputs, dict):
                inputs = {}
            asks = {
                name for name in (placed.get("ask") or [])
                if isinstance(name, str)
            }
            answers = deploy_inputs.get(f"{si}.{bi}") or {}
            if not isinstance(answers, dict):
                answers = {}
            for name in sorted(sensitive):
                if cross_owner and name in asks:
                    answer = answers.get(name)
                    if name not in answers or answer in (None, ""):
                        raise ValueError(
                            f"block {ref!r} field {name!r} requires a deploy-time answer"
                        )
                    continue
                value = inputs.get(name)
                if is_deployer_secret_ref(value):
                    continue
                if value in (None, "") and name in asks:
                    continue
                raise ValueError(
                    f"block {ref!r} field {name!r} must use ask-on-deploy "
                    "or a deployer secret reference"
                )


def reject_cross_owner_hidden_references(recipe, schemas_by_ref, sources_by_ref,
                                         deploy_inputs=None) -> None:
    """Fail a cross-owner plan whose author-controlled text carries deployer refs.

    Every ``{{ secrets.NAME }}`` / ``{{ variable.NAME }}`` reference resolves in the
    DEPLOYER's scope at compile time. The only legitimate cross-owner carriers are a
    sensitive stored input (exactly one full deployer secret reference, enforced by
    validate_public_sensitive_inputs) and the deployer's own deploy-time answers.
    A reference anywhere else — block source templates, non-sensitive schema
    defaults, non-sensitive stored input values — would silently resolve the
    deployer's secrets inside author-controlled text that can ship them anywhere
    (exfiltration primitive), so admission fails closed on any such reference.
    Error messages intentionally contain block/field names only.
    """
    schemas_by_ref = schemas_by_ref if isinstance(schemas_by_ref, dict) else {}
    sources_by_ref = sources_by_ref if isinstance(sources_by_ref, dict) else {}
    deploy_inputs = deploy_inputs if isinstance(deploy_inputs, dict) else {}

    def _carries_ref(value) -> bool:
        if isinstance(value, str):
            return _REF_RE.search(value) is not None
        if isinstance(value, list):
            return any(_carries_ref(item) for item in value)
        return False

    for ref, sources in sources_by_ref.items():
        if any(isinstance(text, str) and _REF_RE.search(text) for text in (sources or ())):
            raise ValueError(
                f"block {ref!r} source must not reference deployer secrets or variables"
            )
    if not isinstance(recipe, list):
        raise ValueError("recipe is unavailable")
    for si, section in enumerate(recipe):
        if not isinstance(section, dict):
            continue
        placements = section.get("blocks") or []
        if not isinstance(placements, list):
            continue
        for bi, placed in enumerate(placements):
            if not isinstance(placed, dict):
                continue
            ref = placed.get("ref")
            schema = schemas_by_ref.get(ref)
            if not isinstance(ref, str) or not isinstance(schema, list):
                continue
            sensitive = {
                field.get("name") for field in schema
                if isinstance(field, dict)
                and isinstance(field.get("name"), str)
                and field.get("type") in ("password", "secret")
            }
            for field in schema:
                if not isinstance(field, dict):
                    continue
                name = field.get("name")
                if not isinstance(name, str) or name in sensitive:
                    continue
                if _carries_ref(field.get("default")):
                    raise ValueError(
                        f"block {ref!r} field {name!r} default must not reference "
                        "deployer secrets or variables"
                    )
            asks = {
                name for name in (placed.get("ask") or [])
                if isinstance(name, str)
            }
            answers = deploy_inputs.get(f"{si}.{bi}") or {}
            if not isinstance(answers, dict):
                answers = {}
            inputs = placed.get("inputs") or {}
            if not isinstance(inputs, dict):
                inputs = {}
            for name, value in inputs.items():
                if not isinstance(name, str) or name in sensitive:
                    continue
                if name in asks and name in answers:
                    continue  # deployer-supplied answer — their own scope by choice
                if _carries_ref(value):
                    raise ValueError(
                        f"block {ref!r} field {name!r} must not reference "
                        "deployer secrets or variables"
                    )


def _placed_blocks(recipe):
    """Yield well-formed block placements, ignoring malformed legacy recipe rows."""
    if not isinstance(recipe, list):
        return
    for section in recipe:
        if not isinstance(section, dict):
            continue
        blocks = section.get("blocks") or []
        if not isinstance(blocks, list):
            continue
        for placed in blocks:
            if isinstance(placed, dict):
                yield placed


def ensure_placement_ids(recipe: list[dict]) -> list[dict]:
    """Copy a recipe, retaining stable placement IDs and assigning missing IDs.

    Positional addresses remain accepted by legacy clients for fresh admission.
    IDs distinguish repeated uses of the same block and survive reorder/edit.
    """
    import uuid
    out = json.loads(json.dumps(recipe))
    seen = set()
    for placed in _placed_blocks(out):
        pid = placed.get("placementId")
        if pid is None:
            pid = "p-" + uuid.uuid4().hex
            placed["placementId"] = pid
        if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", pid) or pid in seen:
            raise ValueError("placementId must be a unique identifier")
        seen.add(pid)
    return out


def positional_deploy_inputs(recipe: list[dict], supplied: dict) -> dict:
    """Resolve stable IDs to current positions, rejecting ambiguous aliases."""
    aliases = {}
    for si, sec in enumerate(recipe):
        if not isinstance(sec, dict) or not isinstance(sec.get("blocks", []), list):
            continue
        for bi, placed in enumerate(sec.get("blocks") or []):
            if isinstance(placed, dict) and isinstance(placed.get("placementId"), str) and placed["placementId"]:
                pid = placed["placementId"]
                if pid in aliases:
                    raise ValueError("duplicate placementId")
                aliases[pid] = f"{si}.{bi}"
    out = {}
    for addr, answers in supplied.items():
        resolved = aliases.get(addr, addr)
        if resolved in out:
            raise ValueError("duplicate answers for the same placement")
        out[resolved] = answers
    return out


def ask_map(recipe: list[dict]) -> dict[str, list[str]]:
    """Ask-on-deploy index: ``{"<sectionIdx>.<blockIdx>": [input names]}`` for
    every placed block carrying a non-empty ``ask`` list."""
    out: dict[str, list[str]] = {}
    for si, sec in enumerate(recipe):
        if not isinstance(sec, dict):
            continue
        blocks = sec.get("blocks") or []
        if not isinstance(blocks, list):
            continue
        for bi, block in enumerate(blocks):
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
    overrides = positional_deploy_inputs(recipe, overrides)
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
        if not isinstance(inputs, dict):
            inputs = {}
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
    if not isinstance(user_inputs, dict):
        user_inputs = {}
    for k, v in user_inputs.items():
        if v not in (None, ""):
            merged[k] = v
    # Defense-in-depth at the SINK: a 'user' value becomes a Linux/PostgreSQL username
    # that lands raw in ansible YAML across several blocks (become_user, owner/group,
    # /home/<user> paths, module name args). Restrict it to a safe username charset so it
    # can never inject sibling YAML keys, traverse paths, or break a scalar — a no-op for
    # real usernames. Cloud-init already shell-quotes every value; this covers ansible too.
    if isinstance(merged.get("user"), str):
        merged["user"] = re.sub(r"[^A-Za-z0-9_-]", "", merged["user"])[:32]
    return merged


def collect_sensitive_inputs(recipe: list, blocks_by_key: dict,
                             secret_lookup: Callable[[str, str], str]) -> set:
    """Resolved values of every password/secret-typed input across the recipe.

    A LITERAL value typed into a password/secret field never passes through
    secret_lookup (only {{ secrets.NAME }} references do), so without collecting these
    the value would be absent from the job-log redaction vault and could leak into
    streamed Ansible output on a failed task. {{ secrets }} refs resolve to their real
    value here too (and are redacted at any length they meet the floor)."""
    out: set = set()
    for placed in _placed_blocks(recipe):
        block = blocks_by_key.get(placed.get("ref", ""))
        if not block:
            continue
        types = _schema_types(block)
        for k, v in _merged_inputs(block, placed).items():
            if types.get(k) in ("password", "secret") and isinstance(v, str) and v:
                val = resolve_secrets(v, secret_lookup)
                if val:
                    out.add(val)
    return out


def _ansible_playbook(recipe: list[dict], blocks_by_key: dict[str, Block],
                      name: str, secret_lookup: Optional[Callable[[str, str], str]] = None) -> str:
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
    for placed in _placed_blocks(recipe):
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
                    secret_lookup: Callable[[str, str], str], name: str = "goblindock") -> str:
    """Runnable Ansible playbook (secrets resolved) for ansible-phase blocks."""
    return _ansible_playbook(recipe, blocks_by_key, name, secret_lookup)


def has_ansible_blocks(recipe: list[dict], blocks_by_key: dict[str, Block]) -> bool:
    for placed in _placed_blocks(recipe):
        b = blocks_by_key.get(placed.get("ref", ""))
        if b and b.phase == "ansible" and b.ansible_template:
            return True
    return False


def compile_playbook(recipe: list[dict], blocks_by_key: dict[str, Block],
                     template_name: str = "recipe") -> str:
    """Read-only preview: the Ansible playbook (post-boot) + a comment listing the
    cloud-init (first-boot) steps. Secrets are masked."""
    pb = _ansible_playbook(recipe, blocks_by_key, template_name)
    ci = []
    for placed in _placed_blocks(recipe):
        b = blocks_by_key.get(placed.get("ref", ""))
        if b and b.phase == "cloudinit" and b.cloudinit_template:
            ci.append(f"#   - {b.name}")
    if ci:
        pb += "\n# --- cloud-init (first-boot) steps in this template ---\n" + "\n".join(ci) + "\n"
    return pb


def compile_cloudinit(
    recipe: list[dict],
    blocks_by_key: dict[str, Block],
    secret_lookup: Callable[[str, str], str],
) -> list[str]:
    """Shell command lines for cloud-init runcmd — only phase='cloudinit' blocks
    (run as root at first boot). Inputs/secrets are shell-quoted (injection-safe)."""
    cmds: list[str] = ["set -e"]
    for placed in _placed_blocks(recipe):
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
    "select", "list", "tags", "bool", "boolean", "toggle",
}
_INPUT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def normalize_input_schema(schema):
    """Copy an accepted authoring/legacy schema with implicit text types made explicit."""
    if not isinstance(schema, list):
        return schema
    normalized = []
    for field in schema:
        if not isinstance(field, dict):
            normalized.append(field)
            continue
        normalized_field = dict(field)
        if normalized_field.get("type") is None:
            normalized_field["type"] = "text"
        normalized.append(normalized_field)
    return normalized


def input_schema_problems(schema, *, require_type=False) -> list[str]:
    """Return authoritative structural/name/type problems for one input schema."""
    if not isinstance(schema, list):
        return ["input schema must be a list of fields"]
    problems: list[str] = []
    seen: set[str] = set()
    for i, field in enumerate(schema):
        if not isinstance(field, dict):
            problems.append(f"input field #{i + 1} must be an object")
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"input field #{i + 1} is missing a name")
            continue
        if not _INPUT_NAME_RE.fullmatch(name):
            problems.append(
                f"input name {name!r} must start with a letter/underscore and use "
                "only letters, digits or underscores"
            )
        if name in seen:
            problems.append(f"duplicate input name {name!r}")
        seen.add(name)
        field_type = field.get("type")
        if field_type is None and require_type:
            problems.append(f"input {name!r} is missing a type")
        elif field_type is not None and (not isinstance(field_type, str) or not field_type):
            problems.append(f"input {name!r} has an invalid type")
        elif field_type is not None and field_type not in _ALLOWED_INPUT_TYPES:
            problems.append(f"input {name!r} has unknown type {field_type!r}")
        if field_type == "select":
            options = field.get("options")
            if not isinstance(options, list) or not options:
                problems.append(f"select input {name!r} needs at least one option")
            elif any(not isinstance(option, str) or not option.strip()
                     for option in options):
                problems.append(f"select input {name!r} options must be non-empty strings")
            elif len(set(options)) != len(options):
                problems.append(f"select input {name!r} has duplicate options")
            elif field.get("default") not in (None, "") and field.get("default") not in options:
                problems.append(f"select input {name!r} default must be one of its options")
    return problems


def _lint_sample(field: dict):
    """A representative sample value for a schema field, used only to render the
    template during a dry-run (never executed)."""
    t = field.get("type") or "text"
    default = field.get("default")
    if default not in (None, ""):
        return default
    if t in ("list", "tags"):
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

    problems.extend(input_schema_problems(schema))

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
    for placed in _placed_blocks(recipe):
        chips.append(placed.get("name", placed.get("ref", "block")))
    return chips

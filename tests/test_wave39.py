"""Wave 39 — UI, accessibility, and onboarding regressions."""
import asyncio
import base64
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave39-test.sqlite3")
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault(
    "GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test")
)

from app import api, seed, serialize as S  # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app.main import app as application  # noqa: E402
from app.models import (  # noqa: E402
    Block,
    Connection,
    Deployment,
    Image,
    Job,
    JobStep,
    Network,
    Secret,
    Template,
    User,
    Variable,
)
from app.security import encrypt, hash_password  # noqa: E402

init_db()


async def _asgi_request(method, path, *, json_body=None, cookie=""):
    """Dependency-free in-process client for the real mounted FastAPI application."""
    body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
    headers = [(b"host", b"testserver")]
    if body:
        headers.extend([
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ])
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))

    request_sent = False
    wait_for_disconnect = asyncio.Event()

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await wait_for_disconnect.wait()
        return {"type": "http.disconnect"}

    messages = []

    async def send(message):
        messages.append(message)

    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 39039),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return {
        "status": start["status"],
        "headers": [(name.decode("latin-1"), value.decode("latin-1"))
                    for name, value in start["headers"]],
        "body": response_body,
    }


def test_ci_runs_ui_behavior_suites_and_fail_closed_syntax_checks_all_20_scripts():
    """CI must execute both UI suites and reject any drift from the 18+2 JS set."""
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    required_contracts = {
        "the complete 18 web + 2 UI-test syntax-check list": (
            "js_files=(web/*.js tests/test_wave37_ui.js tests/test_wave39_ui.js)"
        ),
        "the fail-closed 20-file guard": 'if [ "${#js_files[@]}" -ne 20 ]; then',
        "the syntax-check loop": (
            'for f in "${js_files[@]}"; do node --check "$f"; done'
        ),
        "the Wave 37 UI behavior suite": "node tests/test_wave37_ui.js",
        "the Wave 39 UI behavior suite": "node tests/test_wave39_ui.js",
    }
    missing = [name for name, source in required_contracts.items() if source not in workflow]
    assert not missing, f"CI workflow is missing: {', '.join(missing)}"


def test_beta_build_branch_publishes_without_changing_release_tag_rules():
    """The isolated beta branch must publish its branch-named image via GHCR."""
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github" / "workflows" / "docker-publish.yml"
    ).read_text(encoding="utf-8")

    required_contracts = {
        "the main and isolated beta-build branch trigger": "branches: [main, beta-build]",
        "branch-name image metadata": "type=ref,event=branch",
        "release-only latest image rule": (
            "type=raw,value=latest,enable=${{ startsWith(github.ref, 'refs/tags/v') }}"
        ),
        "multi-architecture beta image": "platforms: linux/amd64,linux/arm64",
    }
    missing = [name for name, source in required_contracts.items() if source not in workflow]
    assert not missing, f"Image publisher is missing: {', '.join(missing)}"


def test_docker_entrypoint_keeps_posix_line_endings_on_windows_checkouts():
    """The Linux entrypoint shebang must survive a Windows Git checkout."""
    root = Path(__file__).resolve().parents[1]
    attributes = (root / ".gitattributes").read_text(encoding="utf-8").splitlines()
    entrypoint = (root / "docker-entrypoint.sh").read_bytes()

    assert "docker-entrypoint.sh text eol=lf" in attributes
    assert entrypoint.startswith(b"#!/bin/sh\n"), "entrypoint shebang is not LF-terminated"
    assert b"\r\n" not in entrypoint, "entrypoint contains Windows CRLF bytes"


def _starter_engine():
    local_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(local_engine)
    return local_engine


def _starter_connection(name):
    return Connection(
        name=name,
        host="pve.example",
        token_id="automation@pve!goblindock",
        node="pve",
    )


@contextmanager
def _local_session_scope(local_engine):
    session = Session(local_engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def _patched_seed_scope(local_engine):
    original = seed.session_scope
    seed.session_scope = lambda: _local_session_scope(local_engine)
    try:
        yield
    finally:
        seed.session_scope = original


class _SriResourceParser(HTMLParser):
    """Preserve resource declarations, including duplicate HTML attributes."""

    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        if any(name in {"src", "href"} for name, _value in attrs):
            self.elements.append((tag, attrs))


_SHA384_INTEGRITY = re.compile(r"sha384-[A-Za-z0-9+/]{64}")


def _sri_validation_errors(index_html, web_root):
    """Validate local vendored SRI declarations and exact response bytes."""
    parser = _SriResourceParser()
    parser.feed(index_html)

    errors = []
    web_root = web_root.resolve()
    vendor_root = (web_root / "vendor").resolve()
    referenced_vendor_paths = []
    seen_vendor_paths = set()

    for tag, attrs in parser.elements:
        attribute_values = {
            name: [value or "" for attr_name, value in attrs if attr_name == name]
            for name in ("src", "href", "integrity")
        }
        for name, values in attribute_values.items():
            if len(values) > 1:
                errors.append(f"<{tag}> has duplicate {name} attribute")

        references = [
            (name, value or "")
            for name, value in attrs
            if name in {"src", "href"}
        ]
        for _attribute_name, reference in references:
            if not reference or reference.startswith("//"):
                continue

            parsed = urlsplit(reference)
            if parsed.scheme.lower() in {"http", "https"}:
                continue

            decoded_path = unquote(parsed.path)
            if "\\" in reference or "\\" in decoded_path:
                errors.append(f"{reference}: local resource path contains a backslash")
                continue
            if parsed.scheme or parsed.netloc or not decoded_path:
                continue

            normalized_path = posixpath.normpath(decoded_path.lstrip("/"))
            asset_path = (web_root / normalized_path).resolve()
            try:
                relative_path = asset_path.relative_to(web_root).as_posix()
            except ValueError:
                errors.append(f"{reference}: local resource path escapes web root")
                continue

            if not (
                relative_path == "vendor" or relative_path.startswith("vendor/")
            ):
                continue
            try:
                asset_path.relative_to(vendor_root)
            except ValueError:
                errors.append(f"{reference}: vendor resource path escapes vendor root")
                continue

            referenced_vendor_paths.append(relative_path)
            if relative_path in seen_vendor_paths:
                errors.append(
                    f"{reference}: duplicate protected vendor path {relative_path}"
                )
            seen_vendor_paths.add(relative_path)

            integrity_values = attribute_values["integrity"]
            if not integrity_values:
                errors.append(
                    f"{reference}: requires exactly one SHA-384 integrity attribute"
                )
                continue
            if len(integrity_values) != 1:
                continue

            expected_integrity = integrity_values[0]
            if _SHA384_INTEGRITY.fullmatch(expected_integrity) is None:
                errors.append(
                    f"{reference}: requires one well-formed SHA-384 integrity value"
                )
                continue

            try:
                asset_bytes = asset_path.read_bytes()
            except OSError as exc:
                errors.append(f"{reference}: unable to read asset: {exc}")
                continue
            actual_integrity = "sha384-" + base64.b64encode(
                hashlib.sha384(asset_bytes).digest()
            ).decode("ascii")
            if actual_integrity != expected_integrity:
                errors.append(
                    f"{reference}: expected {expected_integrity}, got {actual_integrity}"
                )

    actual_vendor_paths = {
        path.relative_to(web_root).as_posix()
        for path in vendor_root.rglob("*")
        if path.is_file()
    }
    referenced_vendor_path_set = set(referenced_vendor_paths)
    if referenced_vendor_path_set != actual_vendor_paths:
        missing = sorted(actual_vendor_paths - referenced_vendor_path_set)
        unexpected = sorted(referenced_vendor_path_set - actual_vendor_paths)
        errors.append(
            f"vendor coverage mismatch: missing={missing}, unexpected={unexpected}"
        )
    return errors


def _replace_once(text, old, new):
    assert text.count(old) == 1, old
    return text.replace(old, new, 1)


def _sha384_integrity(path):
    return "sha384-" + base64.b64encode(
        hashlib.sha384(path.read_bytes()).digest()
    ).decode("ascii")


def _assert_sri_rejected(index_html, expected_error):
    web_root = Path(__file__).resolve().parent.parent / "web"
    errors = _sri_validation_errors(index_html, web_root)
    assert any(expected_error in error for error in errors), errors


def test_sri_rejects_local_vendor_resource_without_integrity():
    web_root = Path(__file__).resolve().parent.parent / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    mutated = _replace_once(
        index_html,
        "</head>",
        '  <script src="vendor/future.js"></script>\n</head>',
    )
    _assert_sri_rejected(mutated, "exactly one SHA-384 integrity")


def test_sri_rejects_local_vendor_resource_with_sha256_only():
    web_root = Path(__file__).resolve().parent.parent / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    mutated = _replace_once(
        index_html,
        "</head>",
        f'  <script src="vendor/future.js" integrity="sha256-{"A" * 44}"></script>\n</head>',
    )
    _assert_sri_rejected(mutated, "well-formed SHA-384 integrity")


def test_sri_rejects_duplicate_resource_replacing_distinct_vendor_asset():
    web_root = Path(__file__).resolve().parent.parent / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    react = (
        '<script src="vendor/react.production.min.js" '
        'integrity="sha384-DGyLxAyjq0f9SPpVevD6IgztCFlnMF6oW/XQGmfe+IsZ8TqEiDrcHkMLKI6fiB/Z" '
        'crossorigin="anonymous"></script>'
    )
    react_dom = (
        '<script src="vendor/react-dom.production.min.js" '
        'integrity="sha384-gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1" '
        'crossorigin="anonymous"></script>'
    )
    mutated = _replace_once(index_html, react_dom, react)
    _assert_sri_rejected(mutated, "duplicate protected vendor path")


def test_sri_rejects_duplicate_integrity_attribute_bad_first_good_second():
    web_root = Path(__file__).resolve().parent.parent / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    good = "sha384-DGyLxAyjq0f9SPpVevD6IgztCFlnMF6oW/XQGmfe+IsZ8TqEiDrcHkMLKI6fiB/Z"
    mutated = _replace_once(
        index_html,
        f'integrity="{good}"',
        f'integrity="sha384-{"A" * 64}" integrity="{good}"',
    )
    _assert_sri_rejected(mutated, "duplicate integrity attribute")


def test_sri_rejects_duplicate_src_attribute_bad_first_good_second():
    web_root = Path(__file__).resolve().parent.parent / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    mutated = _replace_once(
        index_html,
        'src="vendor/react.production.min.js"',
        'src="vendor/not-react.js" src="vendor/react.production.min.js"',
    )
    _assert_sri_rejected(mutated, "duplicate src attribute")


def test_sri_rejects_duplicate_href_attribute_bad_first_good_second():
    web_root = Path(__file__).resolve().parent.parent / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    mutated = _replace_once(
        index_html,
        'href="vendor/xterm/xterm.css"',
        'href="vendor/not-xterm.css" href="vendor/xterm/xterm.css"',
    )
    _assert_sri_rejected(mutated, "duplicate href attribute")


def test_sri_rejects_parent_traversal_outside_web_root():
    repository_root = Path(__file__).resolve().parent.parent
    web_root = repository_root / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    react_dom = (
        '<script src="vendor/react-dom.production.min.js" '
        'integrity="sha384-gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1" '
        'crossorigin="anonymous"></script>'
    )
    traversal = (
        f'<script src="../README.md" integrity="{_sha384_integrity(repository_root / "README.md")}" '
        'crossorigin="anonymous"></script>'
    )
    mutated = _replace_once(index_html, react_dom, traversal)
    _assert_sri_rejected(mutated, "escapes web root")


def test_sri_rejects_percent_encoded_parent_traversal():
    repository_root = Path(__file__).resolve().parent.parent
    web_root = repository_root / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    react_dom = (
        '<script src="vendor/react-dom.production.min.js" '
        'integrity="sha384-gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1" '
        'crossorigin="anonymous"></script>'
    )
    traversal = (
        f'<script src="%2e%2e/README.md" integrity="{_sha384_integrity(repository_root / "README.md")}" '
        'crossorigin="anonymous"></script>'
    )
    mutated = _replace_once(index_html, react_dom, traversal)
    _assert_sri_rejected(mutated, "escapes web root")


def test_sri_rejects_backslash_vendor_reference():
    web_root = Path(__file__).resolve().parent.parent / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    mutated = _replace_once(
        index_html,
        'src="vendor/react-dom.production.min.js" '
        'integrity="sha384-gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1"',
        'src="vendor\\react.production.min.js" '
        'integrity="sha384-DGyLxAyjq0f9SPpVevD6IgztCFlnMF6oW/XQGmfe+IsZ8TqEiDrcHkMLKI6fiB/Z"',
    )
    _assert_sri_rejected(mutated, "backslash")


def test_sri_rejects_windows_device_unc_style_reference():
    web_root = Path(__file__).resolve().parent.parent / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    react_path = (web_root / "vendor/react.production.min.js").resolve()
    device_path = "\\\\?\\" + str(react_path)
    mutated = _replace_once(
        index_html,
        'src="vendor/react-dom.production.min.js" '
        'integrity="sha384-gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1"',
        f'src="{device_path}" '
        'integrity="sha384-DGyLxAyjq0f9SPpVevD6IgztCFlnMF6oW/XQGmfe+IsZ8TqEiDrcHkMLKI6fiB/Z"',
    )
    _assert_sri_rejected(mutated, "backslash")


def test_sri_intentionally_skips_external_http_and_protocol_relative_assets():
    web_root = Path(__file__).resolve().parent.parent / "web"
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    mutated = _replace_once(
        index_html,
        "</head>",
        '  <script src="https://cdn.example/vendor/external.js"></script>\n'
        '  <link href="//cdn.example/vendor/external.css" rel="stylesheet">\n</head>',
    )
    assert _sri_validation_errors(mutated, web_root) == []


def test_sri_reports_exact_pre_fix_six_crlf_mismatches():
    repository_root = Path(__file__).resolve().parent.parent
    web_root = repository_root / "web"
    expected = {
        "vendor/xterm/xterm.css",
        "vendor/novnc/rfb.js",
        "vendor/react.production.min.js",
        "vendor/react-dom.production.min.js",
        "vendor/xterm/xterm.js",
        "vendor/xterm/xterm-addon-fit.js",
    }
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_web_root = Path(temporary_directory) / "web"
        shutil.copytree(web_root, fixture_web_root)
        for relative_path in expected:
            path = fixture_web_root / relative_path
            lf_bytes = path.read_bytes().replace(b"\r\n", b"\n")
            path.write_bytes(lf_bytes.replace(b"\n", b"\r\n"))

        errors = _sri_validation_errors(
            (fixture_web_root / "index.html").read_text(encoding="utf-8"),
            fixture_web_root,
        )

    mismatched_paths = {error.split(":", 1)[0] for error in errors}
    assert mismatched_paths == expected, errors


def test_local_sri_resources_match_exact_working_tree_bytes():
    """Checkout filters must not change bytes protected by browser SRI."""
    repository_root = Path(__file__).resolve().parent.parent
    web_root = repository_root / "web"
    errors = _sri_validation_errors(
        (web_root / "index.html").read_text(encoding="utf-8"), web_root
    )

    assert not errors, "SRI validation failed:\n" + "\n".join(errors)


def test_connection_admin_round_trip_and_public_redaction():
    """Admin editing needs delivery config; the public picker must disclose none of it."""
    token_secret_sentinel = "WAVE39-TOKEN-SECRET-MUST-NOT-SERIALIZE"
    with session_scope() as session:
        conn = Connection(
            name="wave39-connection",
            host="pve.internal.example",
            port=9443,
            token_id="automation@pve!goblindock",
            token_secret_enc=token_secret_sentinel,
            verify_tls=False,
            node="pve-a",
            storage="local-zfs",
            iso_storage="iso-vault",
            snippet_storage="snippets",
            bridge="vmbr9",
            ssh_host="ssh.internal.example",
            ssh_user="automation",
            ssh_key_path="/run/secrets/pve_key",
            max_cores=0,
            max_ram_mb=0,
            max_disk_gb=0,
        )
        session.add(conn)
        session.flush()

        admin = S.connection_dict(session, conn)
        public = S.connection_public_dict(session, conn)

    assert {
        "port": admin["port"],
        "isoStorage": admin["isoStorage"],
        "snippetStorage": admin["snippetStorage"],
        "sshHost": admin["sshHost"],
        "sshUser": admin["sshUser"],
        "sshKeyPath": admin["sshKeyPath"],
        "maxCores": admin["maxCores"],
        "maxRamGb": admin["maxRamGb"],
        "maxDiskGb": admin["maxDiskGb"],
    } == {
        "port": 9443,
        "isoStorage": "iso-vault",
        "snippetStorage": "snippets",
        "sshHost": "ssh.internal.example",
        "sshUser": "automation",
        "sshKeyPath": "/run/secrets/pve_key",
        "maxCores": 0,
        "maxRamGb": 0,
        "maxDiskGb": 0,
    }

    forbidden_public_keys = {
        "url", "host", "port",
        "tokenId", "token_id", "tokenSecret", "token_secret", "token_secret_enc",
        "storage", "isoStorage", "iso_storage", "snippetStorage", "snippet_storage",
        "sshHost", "ssh_host", "sshUser", "ssh_user", "sshKeyPath", "ssh_key_path",
    }
    assert forbidden_public_keys.isdisjoint(public), public
    assert token_secret_sentinel not in json.dumps(admin)
    assert token_secret_sentinel not in json.dumps(public)


def test_authenticated_non_admin_state_endpoint_redacts_operations_and_sensitive_inputs():
    """The mounted endpoint, session auth, tenant filters, and serializers stay closed."""
    suffix = os.urandom(4).hex()
    viewer_email = f"wave39-state-viewer-{suffix}@example.com"
    viewer_password = "StateViewer39!"
    sentinels = {
        "host": f"host-{suffix}.internal.example",
        "token_id": f"wave39-{suffix}@pve!endpoint",
        "token_secret": f"WAVE39-ENDPOINT-TOKEN-{suffix}",
        "storage": f"zfs-private-{suffix}",
        "iso_storage": f"iso-private-{suffix}",
        "snippet_storage": f"snippets-private-{suffix}",
        "ssh_host": f"ssh-{suffix}.internal.example",
        "ssh_user": f"ssh-user-{suffix}",
        "ssh_key_path": f"/run/secrets/wave39-{suffix}",
        "network_topology": f"10.39.{int(suffix[:2], 16)}.0/24",
        "password_input": f"WAVE39-PASSWORD-INPUT-{suffix}",
        "token_input": f"WAVE39-TOKEN-INPUT-{suffix}",
        "password_default": f"WAVE39-PASSWORD-DEFAULT-{suffix}",
        "token_default": f"WAVE39-TOKEN-DEFAULT-{suffix}",
        "admin_secret": f"WAVE39-ADMIN-SECRET-{suffix}",
        "viewer_secret": f"WAVE39-VIEWER-SECRET-{suffix}",
    }

    with session_scope() as session:
        admin = User(
            email=f"wave39-state-admin-{suffix}@example.com",
            name="Wave 39 state admin",
            password_hash=hash_password("AdminState39!"),
            role="admin",
        )
        viewer = User(
            email=viewer_email,
            name="Wave 39 state viewer",
            password_hash=hash_password(viewer_password),
            role="user",
        )
        connection = Connection(
            name=f"wave39-public-target-{suffix}",
            host=sentinels["host"],
            port=9443,
            token_id=sentinels["token_id"],
            token_secret_enc=encrypt(sentinels["token_secret"]),
            verify_tls=False,
            node="pve-public-node",
            storage=sentinels["storage"],
            iso_storage=sentinels["iso_storage"],
            snippet_storage=sentinels["snippet_storage"],
            bridge="vmbr39-private",
            ssh_host=sentinels["ssh_host"],
            ssh_user=sentinels["ssh_user"],
            ssh_key_path=sentinels["ssh_key_path"],
            max_cores=8,
            max_ram_mb=16 * 1024,
            max_disk_gb=200,
        )
        image = Image(
            kind="base",
            name=f"wave39-public-image-{suffix}",
            source_url="https://example.com/wave39-public.img",
            build_status="ready",
        )
        session.add(admin)
        session.add(viewer)
        session.add(connection)
        session.add(image)
        session.flush()

        network = Network(
            connection_id=connection.id,
            name=f"wave39-public-network-{suffix}",
            mode="static",
            bridge="vmbr39-private",
            vlan=339,
            subnet_cidr=sentinels["network_topology"],
            gateway=sentinels["network_topology"].replace("0/24", "1"),
            range_start=sentinels["network_topology"].replace("0/24", "20"),
            range_end=sentinels["network_topology"].replace("0/24", "30"),
            dns="10.39.0.53",
        )
        sensitive_block = Block(
            key=f"c-wave39-state-sensitive-{suffix}",
            kind="custom",
            builtin=False,
            owner_id=admin.id,
            name="Wave 39 sensitive public block",
            input_schema_json=json.dumps([
                {
                    "name": "password",
                    "type": "password",
                    "default": sentinels["password_default"],
                },
                {
                    "name": "token",
                    "type": "secret",
                    "default": sentinels["token_default"],
                },
                {"name": "note", "type": "text", "default": "safe default"},
            ]),
        )
        public_block = Block(
            key=f"b-wave39-state-public-{suffix}",
            builtin=True,
            name="Wave 39 public palette block",
            input_schema_json=json.dumps([
                {"name": "message", "type": "text", "default": "safe default"},
            ]),
        )
        session.add(network)
        session.add(sensitive_block)
        session.add(public_block)
        session.flush()

        template = Template(
            name=f"wave39-public-template-{suffix}",
            owner_id=admin.id,
            public=True,
            recipe_json=json.dumps([{"blocks": [{
                "ref": sensitive_block.key,
                "inputs": {
                    "password": sentinels["password_input"],
                    "token": sentinels["token_input"],
                    "note": "safe public note",
                },
            }]}]),
            base_image_id=image.id,
            connection_id=connection.id,
            network_id=network.id,
        )
        admin_secret = Secret(
            scope="global",
            owner_id=admin.id,
            name=f"WAVE39_ADMIN_SECRET_{suffix}",
            value_enc=encrypt(sentinels["admin_secret"]),
            created_by=admin.id,
        )
        viewer_secret = Secret(
            scope="user",
            owner_id=viewer.id,
            name=f"WAVE39_VIEWER_SECRET_{suffix}",
            value_enc=encrypt(sentinels["viewer_secret"]),
            created_by=viewer.id,
        )
        admin_variable = Variable(
            scope="global",
            owner_id=admin.id,
            name=f"WAVE39_ADMIN_VARIABLE_{suffix}",
            value="admin-only variable",
            created_by=admin.id,
        )
        viewer_variable = Variable(
            scope="user",
            owner_id=viewer.id,
            name=f"WAVE39_VIEWER_VARIABLE_{suffix}",
            value="viewer-visible variable",
            created_by=viewer.id,
        )
        session.add(template)
        session.add(admin_secret)
        session.add(viewer_secret)
        session.add(admin_variable)
        session.add(viewer_variable)
        session.flush()
        fixture = {
            "viewer_id": viewer.id,
            "connection_id": connection.id,
            "connection_name": connection.name,
            "network_id": network.id,
            "network_name": network.name,
            "template_id": template.id,
            "image_name": image.name,
            "sensitive_block": sensitive_block.key,
            "public_block": public_block.key,
            "viewer_secret": viewer_secret.name,
            "admin_secret": admin_secret.name,
            "viewer_variable": viewer_variable.name,
            "admin_variable": admin_variable.name,
        }

    original_px_cache = api._px_cache
    api._px_cache = lambda _connections: {}
    try:
        login = asyncio.run(_asgi_request(
            "POST",
            "/api/auth/login",
            json_body={"email": viewer_email, "password": viewer_password},
        ))
        assert login["status"] == 200, login["body"].decode("utf-8")
        set_cookie = next(
            value for name, value in login["headers"] if name.lower() == "set-cookie"
        )
        response = asyncio.run(_asgi_request(
            "GET", "/api/state", cookie=set_cookie.split(";", 1)[0],
        ))
        assert response["status"] == 200, response["body"].decode("utf-8")
        state = json.loads(response["body"])
    finally:
        api._px_cache = original_px_cache

    assert state["me"]["id"] == fixture["viewer_id"]
    assert state["me"]["isAdmin"] is False
    assert state["USERS"] == []

    public_connection = next(
        row for row in state["CONNECTIONS"]
        if row["connId"] == fixture["connection_id"]
    )
    assert public_connection == {
        "id": f"c-{fixture['connection_id']}",
        "connId": fixture["connection_id"],
        "name": fixture["connection_name"],
        "disabled": False,
        "status": "unknown",
        "version": "—",
        "node": "pve-public-node",
        "vms": 0,
        "maxCores": 8,
        "maxRamGb": 16,
        "maxDiskGb": 200,
    }
    forbidden_connection_keys = {
        "url", "host", "port", "tokenId", "token_id", "tokenSecret",
        "token_secret", "token_secret_enc", "verifyTls", "verify_tls", "storage",
        "isoStorage", "iso_storage", "snippetStorage", "snippet_storage", "bridge",
        "sshHost", "ssh_host", "sshUser", "ssh_user", "sshKeyPath", "ssh_key_path",
    }
    assert forbidden_connection_keys.isdisjoint(public_connection), public_connection

    public_network = next(
        row for row in state["NETWORKS"] if row["netId"] == fixture["network_id"]
    )
    assert public_network["name"] == fixture["network_name"]
    assert public_network["connId"] == fixture["connection_id"]
    assert public_network["rawMode"] == "static"
    assert {"bridge", "vlan", "subnet", "gateway", "rangeStart", "rangeEnd", "dns"}.isdisjoint(
        public_network
    )

    public_template = next(
        row for row in state["TEMPLATES"]
        if row["templateId"] == fixture["template_id"]
    )
    assert (public_template["canEdit"], public_template["canDelete"]) == (False, False)
    assert public_template["deployable"] is True
    assert public_template["base"] == fixture["image_name"]
    assert public_template["connectionId"] == fixture["connection_id"]
    assert public_template["networkId"] == fixture["network_id"]
    assert public_template["recipe"][0]["blocks"][0]["inputs"] == {
        "password": "********",
        "token": "********",
        "note": "safe public note",
    }

    palette_keys = {row["key"] for row in state["PALETTE"]}
    assert fixture["public_block"] in palette_keys
    assert fixture["sensitive_block"] not in palette_keys
    assert fixture["admin_secret"] not in {row["name"] for row in state["SECRETS"]}
    public_viewer_secret = next(
        row for row in state["SECRETS"] if row["name"] == fixture["viewer_secret"]
    )
    assert public_viewer_secret["val"] != sentinels["viewer_secret"]
    assert "•" in public_viewer_secret["val"]
    assert fixture["admin_variable"] not in {row["name"] for row in state["VARIABLES"]}
    public_viewer_variable = next(
        row for row in state["VARIABLES"] if row["name"] == fixture["viewer_variable"]
    )
    assert public_viewer_variable["value"] == "viewer-visible variable"

    serialized_state = json.dumps(state)
    for name, sentinel in sentinels.items():
        assert sentinel not in serialized_state, f"{name} leaked from authenticated /api/state"


def _template_capability_fixture():
    """Create complete templates so capability checks exercise the real serializer."""
    suffix = os.urandom(4).hex()
    with session_scope() as session:
        owner = User(
            email=f"wave39-template-owner-{suffix}@example.com",
            name="Wave 39 template owner",
            password_hash="unused",
        )
        viewer = User(
            email=f"wave39-template-viewer-{suffix}@example.com",
            name="Wave 39 template viewer",
            password_hash="unused",
        )
        admin = User(
            email=f"wave39-template-admin-{suffix}@example.com",
            name="Wave 39 template admin",
            password_hash="unused",
            role="admin",
        )
        block = Block(
            key=f"c-wave39-template-{suffix}",
            kind="custom",
            builtin=False,
            name="Wave 39 password block",
            input_schema_json=json.dumps([
                {"name": "password", "type": "password"},
            ]),
        )
        image = Image(
            kind="base",
            name=f"wave39-template-image-{suffix}",
            source_url="https://example.com/wave39-template.img",
            build_status="ready",
        )
        connection = Connection(
            name=f"wave39-template-connection-{suffix}",
            host="pve.example",
            token_id="automation@pve!goblindock",
            node="pve-a",
        )
        session.add(owner)
        session.add(viewer)
        session.add(admin)
        session.add(block)
        session.add(image)
        session.add(connection)
        session.flush()

        recipe = [{"blocks": [{
            "ref": block.key,
            "inputs": {"password": "WAVE39-TEMPLATE-SECRET", "note": "public"},
        }]}]
        owned = Template(
            name=f"wave39-owned-template-{suffix}",
            description="Capability fixture",
            owner_id=owner.id,
            public=True,
            recipe_json=json.dumps(recipe),
            base_image_id=image.id,
            connection_id=connection.id,
        )
        referenced = Template(
            name=f"wave39-referenced-template-{suffix}",
            owner_id=owner.id,
            public=True,
            recipe_json=json.dumps(recipe),
            base_image_id=image.id,
            connection_id=connection.id,
        )
        system = Template(
            name=f"wave39-system-template-{suffix}",
            owner_id=None,
            public=True,
            recipe_json="[]",
            base_image_id=image.id,
            connection_id=connection.id,
        )
        session.add(owned)
        session.add(referenced)
        session.add(system)
        session.flush()
        session.add(Deployment(
            name=f"wave39-template-deployment-{suffix}",
            owner_id=owner.id,
            template_id=referenced.id,
            image_id=image.id,
            connection_id=connection.id,
        ))
        session.flush()
        return {
            "owner": owner.id,
            "viewer": viewer.id,
            "admin": admin.id,
            "owned": owned.id,
            "referenced": referenced.id,
            "system": system.id,
            "block": block.key,
            "image": image.name,
            "connection": connection.name,
        }


def test_template_capabilities_follow_owner_and_admin_edit_authority():
    fixture = _template_capability_fixture()
    with session_scope() as session:
        template = session.get(Template, fixture["owned"])
        owner = S.template_dict(session, template, viewer=session.get(User, fixture["owner"]))
        admin = S.template_dict(session, template, viewer=session.get(User, fixture["admin"]))
        viewer = S.template_dict(session, template, viewer=session.get(User, fixture["viewer"]))
        system_admin = S.template_dict(
            session,
            session.get(Template, fixture["system"]),
            viewer=session.get(User, fixture["admin"]),
        )
        system_viewer = S.template_dict(
            session,
            session.get(Template, fixture["system"]),
            viewer=session.get(User, fixture["viewer"]),
        )

    assert (owner["canEdit"], owner["canDelete"]) == (True, True)
    assert (admin["canEdit"], admin["canDelete"]) == (True, True)
    assert (viewer["canEdit"], viewer["canDelete"]) == (False, False)
    assert (system_admin["canEdit"], system_admin["canDelete"]) == (True, True)
    assert (system_viewer["canEdit"], system_viewer["canDelete"]) == (False, False)


def test_referenced_owned_template_stays_editable_but_cannot_be_deleted():
    fixture = _template_capability_fixture()
    with session_scope() as session:
        template = session.get(Template, fixture["referenced"])
        owner = S.template_dict(session, template, viewer=session.get(User, fixture["owner"]))
        admin = S.template_dict(session, template, viewer=session.get(User, fixture["admin"]))

    assert owner["used"] == 1
    assert (owner["canEdit"], owner["canDelete"]) == (True, False)
    assert (admin["canEdit"], admin["canDelete"]) == (True, False)


def test_template_capabilities_fail_safe_without_viewer_and_preserve_payload_data():
    fixture = _template_capability_fixture()
    with session_scope() as session:
        template = session.get(Template, fixture["owned"])
        no_viewer = S.template_dict(session, template, viewer=None)
        owner = S.template_dict(session, template, viewer=session.get(User, fixture["owner"]))
        viewer = S.template_dict(session, template, viewer=session.get(User, fixture["viewer"]))

    assert (no_viewer["canEdit"], no_viewer["canDelete"]) == (False, False)
    assert {
        "public": owner["public"],
        "deployable": owner["deployable"],
        "base": owner["base"],
        "location": owner["location"],
        "blocks": owner["blocks"],
    } == {
        "public": True,
        "deployable": True,
        "base": fixture["image"],
        "location": fixture["connection"] + " · pve-a",
        "blocks": [fixture["block"]],
    }
    assert owner["recipe"][0]["blocks"][0]["inputs"] == {
        "password": "WAVE39-TEMPLATE-SECRET",
        "note": "public",
    }
    assert viewer["recipe"][0]["blocks"][0]["inputs"] == {
        "password": "********",
        "note": "public",
    }


def test_job_serializers_preserve_canceled_and_raw_status_without_regressions():
    """Canceled is neutral; failed stays error; live and successful jobs stay unchanged."""
    expected = {
        "canceled": "canceled",
        "failed": "error",
        "running": "working",
        "queued": "working",
        "succeeded": "done",
    }
    serialized = {}

    with session_scope() as session:
        for index, raw_status in enumerate(expected, start=1):
            job = Job(
                type="deploy",
                title=f"Wave 39 {raw_status} job",
                status=raw_status,
                pct=index * 10,
                phase=f"{raw_status.title()} phase",
                error="provisioning failed" if raw_status == "failed" else "",
            )
            session.add(job)
            session.flush()
            session.add(JobStep(
                job_id=job.id,
                seq=0,
                name="Real serializer fixture step",
                state="done" if raw_status == "succeeded" else "pending",
            ))
            session.flush()
            serialized[raw_status] = {
                "brief": S.job_brief(session, job),
                "detail": S.job_detail(session, job),
            }

    for raw_status, ui_status in expected.items():
        assert serialized[raw_status]["brief"]["status"] == ui_status
        assert serialized[raw_status]["brief"]["rawStatus"] == raw_status
        assert serialized[raw_status]["detail"]["status"] == ui_status
        assert serialized[raw_status]["detail"]["rawStatus"] == raw_status


def test_starter_backfill_selects_first_system_row_and_compatible_ordered_location():
    """A lower-ID user row/network must not redirect or suppress the system starter."""
    local_engine = _starter_engine()
    with Session(local_engine) as session:
        owner = User(
            email="wave39-starter-owner@example.com",
            name="Wave 39 starter owner",
            password_hash="unused",
        )
        first_connection = _starter_connection("wave39-first-connection")
        other_connection = _starter_connection("wave39-other-connection")
        session.add(owner)
        session.add(first_connection)
        session.add(other_connection)
        session.flush()

        user_starter = Template(name="AI Dev Box", owner_id=owner.id)
        first_system_starter = Template(name="AI Dev Box", owner_id=None)
        later_system_starter = Template(name="AI Dev Box", owner_id=None)
        session.add(user_starter)
        session.add(first_system_starter)
        session.add(later_system_starter)
        session.flush()

        distracting_network = Network(
            connection_id=other_connection.id,
            name="lower-id-other-connection",
            mode="dhcp",
        )
        selected_network = Network(
            connection_id=first_connection.id,
            name="first-compatible-network",
            mode="dhcp",
        )
        later_network = Network(
            connection_id=first_connection.id,
            name="later-compatible-network",
            mode="dhcp",
        )
        session.add(distracting_network)
        session.add(selected_network)
        session.add(later_network)
        session.commit()

        user_id = user_starter.id
        selected_id = first_system_starter.id
        later_id = later_system_starter.id
        connection_id = first_connection.id
        network_id = selected_network.id
        calls = {"flush": 0, "commit": 0}

        def note_flush(*_args):
            calls["flush"] += 1

        def note_commit(*_args):
            calls["commit"] += 1

        event.listen(session, "before_flush", note_flush)
        event.listen(session, "before_commit", note_commit)
        try:
            assert seed.backfill_starter_template_location(session) is True
            assert seed.backfill_starter_template_location(session) is False
        finally:
            event.remove(session, "before_flush", note_flush)
            event.remove(session, "before_commit", note_commit)

        assert calls == {"flush": 0, "commit": 0}, calls
        assert session.get(Template, selected_id).connection_id == connection_id
        assert session.get(Template, selected_id).network_id == network_id
        assert session.get(Template, user_id).connection_id is None
        assert session.get(Template, user_id).network_id is None
        assert session.get(Template, later_id).connection_id is None
        assert session.get(Template, later_id).network_id is None
        session.rollback()

    with Session(local_engine) as session:
        rolled_back = session.get(Template, selected_id)
        assert rolled_back.connection_id is None
        assert rolled_back.network_id is None


def test_starter_backfill_does_not_partially_write_or_skip_connection_without_network():
    """The first connection wins deterministically, even when a later one has a network."""
    local_engine = _starter_engine()
    with Session(local_engine) as session:
        first_connection = _starter_connection("wave39-no-network-first")
        later_connection = _starter_connection("wave39-network-later")
        starter = Template(name="AI Dev Box", owner_id=None)
        session.add(first_connection)
        session.add(later_connection)
        session.add(starter)
        session.flush()
        session.add(Network(
            connection_id=later_connection.id,
            name="later-only-network",
            mode="dhcp",
        ))
        session.flush()

        assert seed.backfill_starter_template_location(session) is False
        assert starter.connection_id is None
        assert starter.network_id is None
        assert starter not in session.dirty


def test_starter_backfill_never_repairs_any_non_null_connection_choice():
    """Connection is the operator-owned guard, regardless of network validity."""
    cases = ("null", "compatible", "mismatched", "dangling")
    for network_case in cases:
        local_engine = _starter_engine()
        with Session(local_engine) as session:
            chosen_connection = _starter_connection(f"wave39-chosen-{network_case}")
            other_connection = _starter_connection(f"wave39-other-{network_case}")
            session.add(chosen_connection)
            session.add(other_connection)
            session.flush()
            compatible = Network(
                connection_id=chosen_connection.id,
                name=f"wave39-compatible-{network_case}",
                mode="dhcp",
            )
            mismatched = Network(
                connection_id=other_connection.id,
                name=f"wave39-mismatched-{network_case}",
                mode="dhcp",
            )
            session.add(compatible)
            session.add(mismatched)
            session.flush()
            network_id = {
                "null": None,
                "compatible": compatible.id,
                "mismatched": mismatched.id,
                "dangling": 999_999,
            }[network_case]
            starter = Template(
                name="AI Dev Box",
                owner_id=None,
                connection_id=chosen_connection.id,
                network_id=network_id,
            )
            session.add(starter)
            session.flush()
            before = (starter.connection_id, starter.network_id)

            assert seed.backfill_starter_template_location(session) is False
            assert (starter.connection_id, starter.network_id) == before
            assert starter not in session.dirty


def test_starter_backfill_infers_only_a_resolvable_existing_network_owner():
    local_engine = _starter_engine()
    with Session(local_engine) as session:
        connection = _starter_connection("wave39-network-owner")
        session.add(connection)
        session.flush()
        network = Network(
            connection_id=connection.id,
            name="wave39-preserved-network",
            mode="dhcp",
        )
        starter = Template(name="AI Dev Box", owner_id=None)
        session.add(network)
        session.add(starter)
        session.flush()
        starter.network_id = network.id
        session.flush()

        assert seed.backfill_starter_template_location(session) is True
        assert starter.connection_id == connection.id
        assert starter.network_id == network.id
        assert seed.backfill_starter_template_location(session) is False

    for dangling_owner in (False, True):
        local_engine = _starter_engine()
        with Session(local_engine) as session:
            starter = Template(name="AI Dev Box", owner_id=None)
            session.add(starter)
            if dangling_owner:
                network = Network(
                    connection_id=999_998,
                    name="wave39-dangling-owner-network",
                    mode="dhcp",
                )
                session.add(network)
                session.flush()
                preserved_network_id = network.id
            else:
                preserved_network_id = 999_999
            starter.network_id = preserved_network_id
            session.flush()

            assert seed.backfill_starter_template_location(session) is False
            assert starter.connection_id is None
            assert starter.network_id == preserved_network_id
            assert starter not in session.dirty


def test_seed_templates_creates_only_a_location_null_system_definition():
    """A user-owned exact-name row cannot suppress or be mutated by system seeding."""
    local_engine = _starter_engine()
    with Session(local_engine) as session:
        owner = User(
            email="wave39-seed-owner@example.com",
            name="Wave 39 seed owner",
            password_hash="unused",
        )
        connection = _starter_connection("wave39-seed-existing-connection")
        image = Image(
            kind="base",
            name="Wave 39 Ubuntu",
            os_family="ubuntu",
            source_url="https://example.com/ubuntu.img",
        )
        session.add(owner)
        session.add(connection)
        session.add(image)
        session.flush()
        network = Network(
            connection_id=connection.id,
            name="wave39-seed-existing-network",
            mode="dhcp",
        )
        user_starter = Template(
            name="AI Dev Box",
            owner_id=owner.id,
            connection_id=None,
            network_id=None,
        )
        session.add(network)
        session.add(user_starter)
        session.commit()
        user_starter_id = user_starter.id
        image_id = image.id

    with _patched_seed_scope(local_engine):
        seed.seed_templates()

    with Session(local_engine) as session:
        system_starters = session.exec(select(Template).where(
            Template.name == "AI Dev Box",
            Template.owner_id.is_(None),
        ).order_by(Template.id)).all()
        assert len(system_starters) == 1
        assert system_starters[0].base_image_id == image_id
        assert system_starters[0].connection_id is None
        assert system_starters[0].network_id is None
        user_starter = session.get(Template, user_starter_id)
        assert user_starter.connection_id is None
        assert user_starter.network_id is None


def test_run_all_seeds_orders_default_network_before_committed_backfill():
    local_engine = _starter_engine()
    with Session(local_engine) as session:
        connection = _starter_connection("wave39-startup-first-connection")
        session.add(connection)
        session.commit()
        connection_id = connection.id

    with _patched_seed_scope(local_engine):
        seed.run_all_seeds()

    with Session(local_engine) as session:
        starter = session.exec(select(Template).where(
            Template.name == "AI Dev Box",
            Template.owner_id.is_(None),
        ).order_by(Template.id)).one()
        network = session.get(Network, starter.network_id)
        assert starter.connection_id == connection_id
        assert network is not None
        assert network.connection_id == connection_id


def test_add_connection_persists_default_network_then_starter_backfill():
    local_engine = _starter_engine()
    with Session(local_engine) as session:
        admin = User(
            email="wave39-add-connection-admin@example.com",
            name="Wave 39 admin",
            password_hash="unused",
            role="admin",
        )
        starter = Template(name="AI Dev Box", owner_id=None)
        session.add(admin)
        session.add(starter)
        session.commit()
        admin_id = admin.id
        starter_id = starter.id

    with Session(local_engine) as session:
        result = api.add_connection(
            api.ConnBody(
                name="wave39-added-connection",
                host="pve.example",
                token_id="automation@pve!goblindock",
                token_secret="test-only-secret",
                node="pve",
                storage="local-zfs",
            ),
            user=session.get(User, admin_id),
            session=session,
        )
        assert result["ok"] is True

    with Session(local_engine) as session:
        connection = session.exec(select(Connection).where(
            Connection.name == "wave39-added-connection",
        )).one()
        networks = session.exec(select(Network).where(
            Network.connection_id == connection.id,
        ).order_by(Network.id)).all()
        starter = session.get(Template, starter_id)
        assert len(networks) == 1
        assert starter.connection_id == connection.id
        assert starter.network_id == networks[0].id


def test_task7_first_admin_setup_contract_survives_token_gate():
    # main's first-run race fix added a setup token (ignored under GOBLINDOCK_DEV=1,
    # which these tests run with); the flow keeps the same three identity fields.
    assert set(api.SetupBody.model_fields) == {"email", "name", "password", "token"}
    local_engine = _starter_engine()
    request = SimpleNamespace(session={})
    with Session(local_engine) as session:
        result = api.auth_setup(
            api.SetupBody(
                email="wave39-setup-admin@example.com",
                name="Wave 39 Setup Admin",
                password="StrongPass12!",
            ),
            request,
            session,
        )
    assert result["ok"] is True
    assert request.session["uid"]


def _expect_checksum_400(value):
    try:
        api._clean_checksum(value)
    except HTTPException as exc:
        assert exc.status_code == 400, (exc.status_code, exc.detail)
        assert value not in str(exc.detail), "checksum errors must not echo supplied digests"
        return
    raise AssertionError(f"expected checksum rejection for {value!r}")


def test_checksum_validation_normalizes_supported_digests_without_legacy_regression():
    """Creation validation must accept only the five supported bare-hex digest sizes."""
    assert api._clean_checksum("") == ""
    assert api._clean_checksum(" \t\r\n") == ""
    for length in (32, 40, 64, 96, 128):
        assert api._clean_checksum("  " + ("A" * length) + "\t") == "a" * length

    for malformed in (
        "g" * 32,
        "a" * 31,
        "a" * 48,
        ("a" * 16) + " " + ("a" * 16),
        "sha256:" + ("a" * 64),
    ):
        _expect_checksum_400(malformed)

    legacy = "sha256:not-a-bare-legacy-value"
    assert api._checksum_algo(legacy) == "", "deployment context must tolerate legacy rows"
    assert S.base_image_dict(Image(name="legacy", checksum=legacy))["checksum"] == legacy
    assert S.base_image_dict(Image(name="blank", checksum=""))["checksum"] == ""


def test_base_image_create_and_edit_persist_normalized_checksum_atomically():
    """The real route handlers must normalize good input and leave no partial bad edit."""
    suffix = os.urandom(4).hex()
    valid_create = "A" * 64
    valid_edit = "B" * 96
    invalid = "sha256:" + ("c" * 64)

    with session_scope() as session:
        admin = User(
            email=f"wave39-checksum-{suffix}@example.com",
            name="Wave 39 checksum admin",
            password_hash="test-only",
            role="admin",
        )
        session.add(admin)
        session.flush()
        admin_id = admin.id
        before_count = len(session.exec(select(Image)).all())

    original_validate_image_url = api.validate_image_url
    api.validate_image_url = lambda value: value.strip()
    try:
        with Session(engine) as session:
            api.add_base_image(
                api.BaseImageBody(
                    name=f"wave39-checksum-{suffix}",
                    source_url="https://example.com/wave39.img",
                    checksum="  " + valid_create + "\n",
                ),
                user=session.get(User, admin_id),
                session=session,
            )

        with session_scope() as session:
            created = session.exec(select(Image).where(
                Image.name == f"wave39-checksum-{suffix}",
            )).one()
            image_id = created.id
            assert created.checksum == valid_create.lower()
            assert len(session.exec(select(Image)).all()) == before_count + 1

        with Session(engine) as session:
            api.edit_image(
                image_id,
                api.BaseImageEditBody(checksum="\t" + valid_edit + "  "),
                user=session.get(User, admin_id),
                session=session,
            )

        with session_scope() as session:
            edited = session.get(Image, image_id)
            assert edited.checksum == valid_edit.lower()
            original_name = edited.name

        with Session(engine) as session:
            loaded = session.get(Image, image_id)
            try:
                api.edit_image(
                    image_id,
                    api.BaseImageEditBody(name="must-not-persist", checksum=invalid),
                    user=session.get(User, admin_id),
                    session=session,
                )
            except HTTPException as exc:
                assert exc.status_code == 400
                assert invalid not in str(exc.detail)
                assert loaded.name == original_name
                assert loaded.checksum == valid_edit.lower()
            else:
                raise AssertionError("prefixed checksum must reject the edit")

        with Session(engine) as session:
            try:
                api.add_base_image(
                    api.BaseImageBody(
                        name=f"must-not-create-{suffix}",
                        source_url="https://example.com/rejected.img",
                        checksum=invalid,
                    ),
                    user=session.get(User, admin_id),
                    session=session,
                )
            except HTTPException as exc:
                assert exc.status_code == 400
                assert invalid not in str(exc.detail)
            else:
                raise AssertionError("prefixed checksum must reject image creation")

        with session_scope() as session:
            assert session.get(Image, image_id).name == original_name
            assert not session.exec(select(Image).where(
                Image.name == f"must-not-create-{suffix}",
            )).first()
            assert len(session.exec(select(Image)).all()) == before_count + 1
    finally:
        api.validate_image_url = original_validate_image_url


if __name__ == "__main__":
    test_ci_runs_ui_behavior_suites_and_fail_closed_syntax_checks_all_20_scripts()
    test_beta_build_branch_publishes_without_changing_release_tag_rules()
    test_docker_entrypoint_keeps_posix_line_endings_on_windows_checkouts()
    test_sri_rejects_local_vendor_resource_without_integrity()
    test_sri_rejects_local_vendor_resource_with_sha256_only()
    test_sri_rejects_duplicate_resource_replacing_distinct_vendor_asset()
    test_sri_rejects_duplicate_integrity_attribute_bad_first_good_second()
    test_sri_rejects_duplicate_src_attribute_bad_first_good_second()
    test_sri_rejects_duplicate_href_attribute_bad_first_good_second()
    test_sri_rejects_parent_traversal_outside_web_root()
    test_sri_rejects_percent_encoded_parent_traversal()
    test_sri_rejects_backslash_vendor_reference()
    test_sri_rejects_windows_device_unc_style_reference()
    test_sri_intentionally_skips_external_http_and_protocol_relative_assets()
    test_sri_reports_exact_pre_fix_six_crlf_mismatches()
    test_local_sri_resources_match_exact_working_tree_bytes()
    test_connection_admin_round_trip_and_public_redaction()
    test_authenticated_non_admin_state_endpoint_redacts_operations_and_sensitive_inputs()
    test_template_capabilities_follow_owner_and_admin_edit_authority()
    test_referenced_owned_template_stays_editable_but_cannot_be_deleted()
    test_template_capabilities_fail_safe_without_viewer_and_preserve_payload_data()
    test_job_serializers_preserve_canceled_and_raw_status_without_regressions()
    test_starter_backfill_selects_first_system_row_and_compatible_ordered_location()
    test_starter_backfill_does_not_partially_write_or_skip_connection_without_network()
    test_starter_backfill_never_repairs_any_non_null_connection_choice()
    test_starter_backfill_infers_only_a_resolvable_existing_network_owner()
    test_seed_templates_creates_only_a_location_null_system_definition()
    test_run_all_seeds_orders_default_network_before_committed_backfill()
    test_add_connection_persists_default_network_then_starter_backfill()
    test_task7_first_admin_setup_contract_survives_token_gate()
    test_checksum_validation_normalizes_supported_digests_without_legacy_regression()
    test_base_image_create_and_edit_persist_normalized_checksum_atomically()
    print("\nALL WAVE 39 UNIT TESTS PASSED")

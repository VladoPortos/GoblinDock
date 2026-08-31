"""Wave 39 — UI, accessibility, and onboarding regressions."""
import base64
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException
from sqlmodel import Session, select

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

from app import api, serialize as S  # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app.models import Connection, Image, User  # noqa: E402

init_db()


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
    test_checksum_validation_normalizes_supported_digests_without_legacy_regression()
    test_base_image_create_and_edit_persist_normalized_checksum_atomically()
    print("\nALL WAVE 39 UNIT TESTS PASSED")

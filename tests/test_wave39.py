"""Wave 39 — UI, accessibility, and onboarding regressions."""
import base64
import hashlib
import json
import os
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

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

from app import serialize as S  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Connection  # noqa: E402

init_db()


class _SriResourceParser(HTMLParser):
    """Collect local resources whose exact response bytes are SRI-protected."""

    def __init__(self):
        super().__init__()
        self.resources = []

    def handle_starttag(self, _tag, attrs):
        attributes = dict(attrs)
        integrity = attributes.get("integrity", "")
        if not integrity.startswith("sha384-"):
            return

        for attribute in ("src", "href"):
            reference = attributes.get(attribute)
            if not reference or reference.startswith("//"):
                continue
            parsed = urlsplit(reference)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            self.resources.append((unquote(parsed.path), integrity))


def test_local_sri_resources_match_exact_working_tree_bytes():
    """Checkout filters must not change bytes protected by browser SRI."""
    repository_root = Path(__file__).resolve().parent.parent
    web_root = repository_root / "web"
    parser = _SriResourceParser()
    parser.feed((web_root / "index.html").read_text(encoding="utf-8"))

    assert len(parser.resources) == 11, parser.resources

    mismatches = []
    for reference, expected_integrity in parser.resources:
        asset_path = web_root / reference.lstrip("/")
        actual_integrity = "sha384-" + base64.b64encode(
            hashlib.sha384(asset_path.read_bytes()).digest()
        ).decode("ascii")
        if actual_integrity != expected_integrity:
            mismatches.append(
                f"{reference}: expected {expected_integrity}, got {actual_integrity}"
            )

    assert not mismatches, "SRI mismatch for local assets:\n" + "\n".join(mismatches)


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


if __name__ == "__main__":
    test_local_sri_resources_match_exact_working_tree_bytes()
    test_connection_admin_round_trip_and_public_redaction()
    print("\nALL WAVE 39 UNIT TESTS PASSED")

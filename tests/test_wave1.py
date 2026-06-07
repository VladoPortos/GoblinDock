"""Wave 1 — secret-log redaction, name validation, disk bounds, checksum inference.

Run: GOBLINDOCK_SECRET_KEY=<64hex> GOBLINDOCK_DATA_DIR=/tmp/gd-data-test \
     .venv/bin/python tests/test_wave1.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_redactor():
    from app.worker import _redactor
    r = _redactor({"supersecret", "abc"})           # "abc" (len 3) is below the 4-char floor
    assert r("DB_PASS=supersecret done") == "DB_PASS=*** done"
    assert r("abc stays untouched") == "abc stays untouched"   # short value not masked
    # longest-first: a value that is a superstring masks fully, not partially
    r2 = _redactor({"secret", "secretvalue"})
    assert r2("x=secretvalue y") == "x=*** y", r2("x=secretvalue y")
    # multiline secret (e.g. an SSH private key) is masked line-by-line, since stdout
    # is processed one line at a time
    key = "-----BEGIN KEY-----\nAAAABBBBCCCCDDDD\n-----END KEY-----"
    r3 = _redactor({key})
    assert "AAAABBBBCCCCDDDD" not in r3("ok: [h] => msg: AAAABBBBCCCCDDDD leaked")
    # empty vault is a no-op
    assert _redactor(set())("nothing to mask") == "nothing to mask"
    print("test_redactor OK")


def test_clean_name():
    from app.api import _clean_name
    from fastapi import HTTPException
    assert _clean_name("my-vm_1.2") == "my-vm_1.2"
    assert _clean_name("  spaced name ") == "spaced name"
    bad = [
        "vm\nruncmd: [touch /x]",   # YAML-injection newline (the F15 vector)
        "a:b",                       # colon — YAML key char
        "x\r\nhostname: evil",
        "-leadingdash",
        "",
        "x" * 64,                    # too long (>63)
    ]
    for b in bad:
        try:
            _clean_name(b)
            assert False, f"expected reject for {b!r}"
        except HTTPException as e:
            assert e.status_code == 400
    print("test_clean_name OK")


def test_checksum_algo():
    from app.api import _checksum_algo
    assert _checksum_algo("a" * 64) == "sha256"
    assert _checksum_algo("b" * 32) == "md5"
    assert _checksum_algo("c" * 128) == "sha512"
    assert _checksum_algo("d" * 40) == "sha1"
    assert _checksum_algo("") == ""
    assert _checksum_algo("nothex!!" * 8) == ""   # non-hex → no algorithm
    assert _checksum_algo("a" * 50) == ""         # unknown length → none
    print("test_checksum_algo OK")


def test_disk_bounds():
    from app.api import DeployBody, TemplateBody
    from pydantic import ValidationError
    # valid
    DeployBody(templateId=1, disk=20, cpu=2, ram=4)
    TemplateBody(name="r", disk=10)
    # negative / zero / absurd rejected
    for kwargs in ({"templateId": 1, "disk": -5}, {"templateId": 1, "disk": 0},
                   {"templateId": 1, "disk": 999999}, {"templateId": 1, "cpu": 0},
                   {"templateId": 1, "ram": 99999}):
        try:
            DeployBody(**kwargs)
            assert False, f"expected ValidationError for {kwargs}"
        except ValidationError:
            pass
    print("test_disk_bounds OK")


if __name__ == "__main__":
    test_redactor()
    test_clean_name()
    test_checksum_algo()
    test_disk_bounds()
    print("\nALL WAVE 1 UNIT TESTS PASSED")

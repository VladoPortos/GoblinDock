"""Wave 7 — stale-image cleanup report.

The stale-image endpoint (GET /api/images/stale) was removed in the
templates-only refactor (Task 3). This file is retained as a placeholder;
the frontend Stale Images page is removed in Tasks 4-6.

Run: GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave7.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_stale_images_endpoint_removed():
    """Verify the stale_images function no longer exists in app.api."""
    import app.api as api
    assert not hasattr(api, "stale_images"), (
        "stale_images endpoint should have been removed in the templates-only refactor"
    )
    print("test_stale_images_endpoint_removed OK")


if __name__ == "__main__":
    test_stale_images_endpoint_removed()
    print("\nAll wave-7 tests passed.")

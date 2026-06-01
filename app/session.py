"""A drop-in SessionMiddleware whose signer tolerates a small *backward* clock skew.

Starlette's SessionMiddleware signs the session cookie with a timestamp and, via
itsdangerous, rejects any cookie whose timestamp is in the future ("Signature age
N < 0 seconds"). It re-signs the cookie (fresh timestamp) on every response.

On hosts whose wall clock occasionally jumps *backward* — extremely common under
WSL2, and possible on VMs after an NTP step or live-migration — a cookie that the
previous response just re-signed then looks "from the future" for a few seconds.
itsdangerous raises SignatureExpired, SessionMiddleware decodes the session as
empty, and the user gets a burst of spurious 401s (which the SPA used to turn into
a drop-to-login). A page refresh fixes it because by then the clock has caught up.

We keep the normal forward max-age expiry but widen the lower bound by a small
leeway, so a slightly future-dated cookie is still accepted. This is the same
clock-skew allowance JWT validators conventionally apply (~300s).
"""
from __future__ import annotations

from itsdangerous import SignatureExpired, TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

# Tolerated future-dating of a session cookie (seconds). Backward clock jumps up to
# this size no longer invalidate a freshly-signed session. Negligible impact on the
# 14-day expiry; it only means a cookie may be accepted up to this long "early".
CLOCK_SKEW_LEEWAY = 300


class _SkewTolerantSigner(TimestampSigner):
    def unsign(self, signed_value, max_age=None, return_timestamp=False):  # type: ignore[override]
        # Verify the signature and read the timestamp WITHOUT itsdangerous' built-in
        # age checks (max_age=None), then enforce max-age ourselves while allowing a
        # small backward clock skew on the future side.
        value, ts = super().unsign(signed_value, max_age=None, return_timestamp=True)
        if max_age is not None:
            age = self.get_timestamp() - int(ts.timestamp())
            if age > max_age:
                raise SignatureExpired(f"Signature age {age} > {max_age} seconds")
            if age < -CLOCK_SKEW_LEEWAY:
                raise SignatureExpired(f"Signature age {age} < -{CLOCK_SKEW_LEEWAY} seconds (clock skew)")
        if return_timestamp:
            return value, ts
        return value


class ClockSkewTolerantSessionMiddleware(SessionMiddleware):
    """SessionMiddleware that swaps in the skew-tolerant signer. Same constructor."""

    def __init__(self, app, secret_key, **kwargs):  # noqa: ANN001
        super().__init__(app, secret_key, **kwargs)
        self.signer = _SkewTolerantSigner(str(secret_key))

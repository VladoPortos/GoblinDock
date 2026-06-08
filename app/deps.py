"""Auth dependencies — session-cookie based."""
from __future__ import annotations

from datetime import timezone

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select

from .db import get_session
from .models import User, utcnow
from .security import WIDGET_KEY_PREFIX, hash_widget_key


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    user = session.get(User, uid)
    if not user or user.disabled:
        # Fail closed, but do NOT request.session.clear() here. Clearing empties the
        # session, which makes Starlette delete the session cookie — turning even a
        # transient/stale read into a hard logout that a page refresh can't recover
        # from. A genuinely deleted/disabled user simply keeps getting 401s on every
        # request; the cookie is only cleared on an explicit POST /api/auth/logout.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    # Session versioning: a password change/reset bumps user.session_epoch, so a cookie
    # signed under an older epoch is no longer valid (revokes stolen/old sessions).
    if request.session.get("sv", 0) != user.session_epoch:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user


# A Homepage tile polls every ~15s; don't rewrite widget_key_last_used on every
# request — throttle the write to once every few minutes to avoid WAL churn.
_WIDGET_KEY_LAST_USED_THROTTLE_S = 300


def widget_key_user(request: Request, session: Session = Depends(get_session)) -> User:
    """Authenticate a request by its read-only widget API key (``X-API-Key``).

    Independent of the session cookie — this never reads ``request.session``. Every
    failure returns an identical 401 (no "key exists"/"disabled" oracle), and the
    high-entropy token makes the direct hash lookup safe from timing enumeration.
    """
    token = request.headers.get("x-api-key") or ""
    if not token.startswith(WIDGET_KEY_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
    user = session.exec(
        select(User).where(User.widget_key_hash == hash_widget_key(token))
    ).first()
    if not user or user.disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
    _touch_widget_key_last_used(session, user)
    return user


def _touch_widget_key_last_used(session: Session, user: User) -> None:
    now = utcnow()
    last = user.widget_key_last_used
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)  # SQLite hands back naive UTC
    if last is None or (now - last).total_seconds() >= _WIDGET_KEY_LAST_USED_THROTTLE_S:
        user.widget_key_last_used = now
        session.add(user)
        session.commit()

"""Auth dependencies — session-cookie based."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

from .db import get_session
from .models import User


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

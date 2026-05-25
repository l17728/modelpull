"""Local username/password authentication service."""
from __future__ import annotations

import logging

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.local_credentials import LocalCredential
from dlw.db.models.tenant import User

logger = logging.getLogger(__name__)
_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


async def bootstrap_admin(
    session: AsyncSession,
    username: str,
    password: str,
    tenant_id: int = 1,
) -> bool:
    """Create admin local credential if it does not exist. Returns True if created."""
    existing = (await session.execute(
        select(LocalCredential).where(LocalCredential.username == username)
    )).scalar_one_or_none()
    if existing is not None:
        return False
    user = User(
        tenant_id=tenant_id,
        oidc_subject=f"local:{username}",
        email=None,
        role="system_admin",
        is_active=True,
    )
    session.add(user)
    await session.flush()
    cred = LocalCredential(
        user_id=user.id,
        tenant_id=tenant_id,
        role="system_admin",
        username=username,
        password_hash=hash_password(password),
        must_change_password=False,
        project_ids=None,
    )
    session.add(cred)
    await session.flush()
    return True


async def authenticate(
    session: AsyncSession,
    username: str,
    password: str,
) -> LocalCredential | None:
    cred = (await session.execute(
        select(LocalCredential).where(LocalCredential.username == username)
    )).scalar_one_or_none()
    if cred is None:
        return None
    if not verify_password(cred.password_hash, password):
        return None
    return cred


_VALID_ROLES = frozenset(
    {"system_admin", "tenant_admin", "tenant_operator", "tenant_viewer"})


async def create_user(
    session: AsyncSession,
    username: str,
    password: str,
    tenant_id: int,
    role: str,
    project_ids: list[int] | None = None,
) -> LocalCredential:
    if role not in _VALID_ROLES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_ROLE",
                    "message": f"role must be one of {sorted(_VALID_ROLES)}"})
    existing = (await session.execute(
        select(LocalCredential).where(LocalCredential.username == username)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "USERNAME_TAKEN",
                    "message": f"username '{username}' already exists"})
    user = User(
        tenant_id=tenant_id,
        oidc_subject=f"local:{username}",
        email=None,
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    cred = LocalCredential(
        user_id=user.id,
        tenant_id=tenant_id,
        role=role,
        username=username,
        password_hash=hash_password(password),
        must_change_password=True,
        project_ids=project_ids or None,
    )
    session.add(cred)
    await session.flush()
    return cred


async def change_password(
    session: AsyncSession,
    user_id: int,
    old_password: str,
    new_password: str,
) -> None:
    cred = (await session.execute(
        select(LocalCredential).where(LocalCredential.user_id == user_id)
    )).scalar_one_or_none()
    if cred is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "NO_LOCAL_CREDENTIAL",
                    "message": "no local credential for this account"})
    if not verify_password(cred.password_hash, old_password):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": "WRONG_PASSWORD",
                    "message": "current password is incorrect"})
    cred.password_hash = hash_password(new_password)
    cred.must_change_password = False


async def reset_password(
    session: AsyncSession,
    user_id: int,
    new_password: str,
) -> None:
    cred = (await session.execute(
        select(LocalCredential).where(LocalCredential.user_id == user_id)
    )).scalar_one_or_none()
    if cred is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "NO_LOCAL_CREDENTIAL",
                    "message": "no local credential for this user"})
    cred.password_hash = hash_password(new_password)
    cred.must_change_password = True


async def list_users(session: AsyncSession) -> list[LocalCredential]:
    rows = (await session.execute(
        select(LocalCredential).order_by(LocalCredential.id)
    )).scalars().all()
    return list(rows)

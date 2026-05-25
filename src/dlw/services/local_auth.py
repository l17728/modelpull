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
        logger.debug("bootstrap_admin: admin '%s' already exists, skipping", username)
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
    logger.info("bootstrap_admin: created local admin user '%s' (user_id=%d)", username, user.id)
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
        logger.warning("authenticate: unknown username '%s'", username)
        return None
    if not verify_password(cred.password_hash, password):
        logger.warning("authenticate: wrong password for username '%s'", username)
        return None
    logger.debug("authenticate: successful login for '%s' (user_id=%d)", username, cred.user_id)
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
        logger.warning("create_user: invalid role '%s' requested", role)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_ROLE",
                    "message": f"role must be one of {sorted(_VALID_ROLES)}"})
    existing = (await session.execute(
        select(LocalCredential).where(LocalCredential.username == username)
    )).scalar_one_or_none()
    if existing is not None:
        logger.warning("create_user: username '%s' already exists", username)
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
    logger.info("create_user: created local user '%s' (user_id=%d tenant_id=%d role=%s)",
                username, user.id, tenant_id, role)
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
        logger.warning("change_password: no local credential for user_id=%d", user_id)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "NO_LOCAL_CREDENTIAL",
                    "message": "no local credential for this account"})
    if not verify_password(cred.password_hash, old_password):
        logger.warning("change_password: wrong current password for user_id=%d", user_id)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": "WRONG_PASSWORD",
                    "message": "current password is incorrect"})
    cred.password_hash = hash_password(new_password)
    cred.must_change_password = False
    logger.info("change_password: password updated for user_id=%d (must_change_password cleared)", user_id)


async def reset_password(
    session: AsyncSession,
    user_id: int,
    new_password: str,
) -> None:
    cred = (await session.execute(
        select(LocalCredential).where(LocalCredential.user_id == user_id)
    )).scalar_one_or_none()
    if cred is None:
        logger.warning("reset_password: no local credential for user_id=%d", user_id)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "NO_LOCAL_CREDENTIAL",
                    "message": "no local credential for this user"})
    cred.password_hash = hash_password(new_password)
    cred.must_change_password = True
    logger.info("reset_password: password reset for user_id=%d (must_change_password set)", user_id)


async def list_users(session: AsyncSession) -> list[LocalCredential]:
    rows = (await session.execute(
        select(LocalCredential).order_by(LocalCredential.id)
    )).scalars().all()
    return list(rows)

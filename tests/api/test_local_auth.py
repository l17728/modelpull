"""Local username/password authentication — service + API tests."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


# ---------------------------------------------------------------------------
# Module-level DB bootstrap (tables + tenant/project seed)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401 — registers all ORM models with Base.metadata
    from dlw.db.models.tenant import Project, Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="default", display_name="Default"))
        s.add(Tenant(id=10, slug="local-auth-test", display_name="LocalAuthTest"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="default"))
        s.add(Project(id=10, tenant_id=10, name="default"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ---------------------------------------------------------------------------
# Settings fixture: non-dev mode, predictable JWT secret
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "false")
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", "unit-local-auth-secret-32-bytes!!")
    monkeypatch.setenv("DLW_AUTH_TENANT_RULES_JSON", "[]")
    monkeypatch.delenv("DLW_ADMIN_INITIAL_PASSWORD", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# ASGI test client
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: issue a system-admin JWT directly (bypasses HTTP login)
# ---------------------------------------------------------------------------

def _admin_token(user_id: int = 1) -> str:
    from dlw.auth.principal import issue_system_jwt
    settings = get_settings()
    return issue_system_jwt(
        secret=settings.system_jwt_secret,
        user_id=user_id,
        tenant_id=1,
        role="system_admin",
        project_ids=[],
        ttl_seconds=3600,
    )


def _user_token(user_id: int, tenant_id: int = 10, role: str = "tenant_operator") -> str:
    from dlw.auth.principal import issue_system_jwt
    settings = get_settings()
    return issue_system_jwt(
        secret=settings.system_jwt_secret,
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        project_ids=[],
        ttl_seconds=3600,
    )


# ===========================================================================
# SERVICE-LEVEL TESTS (no HTTP, direct DB session)
# ===========================================================================

@pytest.mark.slow
class TestServiceBootstrapAdmin:
    async def test_creates_admin_on_first_call(self, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        created = await bootstrap_admin(db_session, "svc_admin1", "strongpassword1")
        await db_session.commit()
        assert created is True

    async def test_idempotent_second_call(self, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "svc_admin2", "strongpassword2")
        await db_session.commit()
        created2 = await bootstrap_admin(db_session, "svc_admin2", "differentpassword")
        assert created2 is False

    async def test_admin_has_system_admin_role(self, db_session: AsyncSession):
        from sqlalchemy import select
        from dlw.db.models.local_credentials import LocalCredential
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "svc_admin3", "strongpassword3")
        await db_session.commit()
        cred = (await db_session.execute(
            select(LocalCredential).where(LocalCredential.username == "svc_admin3")
        )).scalar_one()
        assert cred.role == "system_admin"
        assert cred.must_change_password is False

    async def test_admin_user_record_has_local_oidc_subject(self, db_session: AsyncSession):
        from sqlalchemy import select
        from dlw.db.models.local_credentials import LocalCredential
        from dlw.db.models.tenant import User
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "svc_admin4", "strongpassword4")
        await db_session.commit()
        cred = (await db_session.execute(
            select(LocalCredential).where(LocalCredential.username == "svc_admin4")
        )).scalar_one()
        user = await db_session.get(User, cred.user_id)
        assert user is not None
        assert user.oidc_subject == "local:svc_admin4"


@pytest.mark.slow
class TestServiceAuthenticate:
    async def test_valid_credentials_return_cred(self, db_session: AsyncSession):
        from dlw.services.local_auth import authenticate, bootstrap_admin
        await bootstrap_admin(db_session, "auth_ok_user", "correct-pass-1!")
        await db_session.commit()
        cred = await authenticate(db_session, "auth_ok_user", "correct-pass-1!")
        assert cred is not None
        assert cred.username == "auth_ok_user"

    async def test_wrong_password_returns_none(self, db_session: AsyncSession):
        from dlw.services.local_auth import authenticate, bootstrap_admin
        await bootstrap_admin(db_session, "auth_bad_user", "correct-pass-2!")
        await db_session.commit()
        cred = await authenticate(db_session, "auth_bad_user", "wrong-pass")
        assert cred is None

    async def test_unknown_username_returns_none(self, db_session: AsyncSession):
        from dlw.services.local_auth import authenticate
        cred = await authenticate(db_session, "nobody_here", "any-pass")
        assert cred is None


@pytest.mark.slow
class TestServiceCreateUser:
    async def test_creates_user_with_must_change_password(self, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        cred = await create_user(db_session, "new_op", "initpass12", 10, "tenant_operator")
        await db_session.commit()
        assert cred.must_change_password is True
        assert cred.role == "tenant_operator"
        assert cred.tenant_id == 10

    async def test_duplicate_username_raises_409(self, db_session: AsyncSession):
        from fastapi import HTTPException
        from dlw.services.local_auth import create_user
        await create_user(db_session, "dup_user", "initpass12", 10, "tenant_viewer")
        await db_session.commit()
        with pytest.raises(HTTPException) as exc:
            await create_user(db_session, "dup_user", "otherpass12", 10, "tenant_operator")
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "USERNAME_TAKEN"

    async def test_invalid_role_raises_422(self, db_session: AsyncSession):
        from fastapi import HTTPException
        from dlw.services.local_auth import create_user
        with pytest.raises(HTTPException) as exc:
            await create_user(db_session, "bad_role_user", "initpass12", 10, "ghost")
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "INVALID_ROLE"

    async def test_project_ids_stored(self, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        cred = await create_user(db_session, "proj_user", "initpass12", 10,
                                 "tenant_operator", project_ids=[1, 2, 3])
        await db_session.commit()
        assert cred.project_ids == [1, 2, 3]

    async def test_all_valid_roles_accepted(self, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        for i, role in enumerate(
            ["system_admin", "tenant_admin", "tenant_operator", "tenant_viewer"]
        ):
            cred = await create_user(
                db_session, f"role_test_{i}", "initpass12", 10, role)
            assert cred.role == role
        await db_session.commit()


@pytest.mark.slow
class TestServiceChangePassword:
    async def test_correct_old_password_changes_hash(self, db_session: AsyncSession):
        from dlw.services.local_auth import authenticate, bootstrap_admin, change_password
        await bootstrap_admin(db_session, "cpw_user1", "oldpass1234")
        await db_session.commit()
        cred_before = await authenticate(db_session, "cpw_user1", "oldpass1234")
        assert cred_before is not None
        await change_password(db_session, cred_before.user_id, "oldpass1234", "newpass5678")
        await db_session.commit()
        assert await authenticate(db_session, "cpw_user1", "newpass5678") is not None
        assert await authenticate(db_session, "cpw_user1", "oldpass1234") is None

    async def test_clears_must_change_password(self, db_session: AsyncSession):
        from dlw.services.local_auth import change_password, create_user
        cred = await create_user(db_session, "cpw_flag_user", "initpass12", 10, "tenant_operator")
        await db_session.commit()
        assert cred.must_change_password is True
        await change_password(db_session, cred.user_id, "initpass12", "newpass5678")
        await db_session.commit()
        assert cred.must_change_password is False

    async def test_wrong_old_password_raises_401(self, db_session: AsyncSession):
        from fastapi import HTTPException
        from dlw.services.local_auth import bootstrap_admin, change_password
        await bootstrap_admin(db_session, "cpw_bad_user", "oldpass1234")
        await db_session.commit()
        with pytest.raises(HTTPException) as exc:
            await change_password(db_session, 9999999, "wrong", "newpass5678")
        assert exc.value.status_code == 404

    async def test_nonexistent_user_id_raises_404(self, db_session: AsyncSession):
        from fastapi import HTTPException
        from dlw.services.local_auth import change_password
        with pytest.raises(HTTPException) as exc:
            await change_password(db_session, 9999998, "any", "newpass5678")
        assert exc.value.status_code == 404


@pytest.mark.slow
class TestServiceResetPassword:
    async def test_admin_can_reset_any_password(self, db_session: AsyncSession):
        from dlw.services.local_auth import authenticate, create_user, reset_password
        cred = await create_user(db_session, "rpw_user1", "initpass12", 10, "tenant_viewer")
        await db_session.commit()
        await reset_password(db_session, cred.user_id, "newadminset99")
        await db_session.commit()
        assert await authenticate(db_session, "rpw_user1", "newadminset99") is not None

    async def test_reset_sets_must_change_password(self, db_session: AsyncSession):
        from dlw.services.local_auth import create_user, reset_password
        cred = await create_user(db_session, "rpw_user2", "initpass12", 10, "tenant_viewer")
        await db_session.commit()
        await reset_password(db_session, cred.user_id, "newadminset99")
        await db_session.commit()
        assert cred.must_change_password is True

    async def test_nonexistent_user_raises_404(self, db_session: AsyncSession):
        from fastapi import HTTPException
        from dlw.services.local_auth import reset_password
        with pytest.raises(HTTPException) as exc:
            await reset_password(db_session, 9999997, "newpass5678")
        assert exc.value.status_code == 404


@pytest.mark.slow
class TestServiceListUsers:
    async def test_returns_all_created_users(self, db_session: AsyncSession):
        from dlw.services.local_auth import create_user, list_users
        before = await list_users(db_session)
        await create_user(db_session, "list_u1", "initpass12", 10, "tenant_viewer")
        await create_user(db_session, "list_u2", "initpass12", 10, "tenant_operator")
        await db_session.commit()
        after = await list_users(db_session)
        assert len(after) >= len(before) + 2
        names = {c.username for c in after}
        assert "list_u1" in names
        assert "list_u2" in names


# ===========================================================================
# API-LEVEL TESTS (HTTP through ASGI client)
# ===========================================================================

@pytest.mark.slow
class TestApiLogin:
    async def test_login_with_valid_credentials(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "api_admin_login", "securepass99")
        await db_session.commit()
        r = await client.post("/api/v1/auth/local/login",
                              json={"username": "api_admin_login", "password": "securepass99"})
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["role"] == "system_admin"
        assert body["must_change_password"] is False

    async def test_login_wrong_password_returns_401(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "api_bad_login", "rightpass99")
        await db_session.commit()
        r = await client.post("/api/v1/auth/local/login",
                              json={"username": "api_bad_login", "password": "wrongpass"})
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "INVALID_CREDENTIALS"

    async def test_login_unknown_user_returns_401(self, client):
        r = await client.post("/api/v1/auth/local/login",
                              json={"username": "ghost_user", "password": "any"})
        assert r.status_code == 401

    async def test_login_returns_must_change_password_flag(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        cred = await create_user(db_session, "api_must_change", "initpass12", 10, "tenant_operator")
        await db_session.commit()
        r = await client.post("/api/v1/auth/local/login",
                              json={"username": "api_must_change", "password": "initpass12"})
        assert r.status_code == 200
        assert r.json()["must_change_password"] is True

    async def test_token_is_valid_jwt(self, client, db_session: AsyncSession):
        import jwt as _pyjwt
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "api_jwt_check", "securepass99")
        await db_session.commit()
        r = await client.post("/api/v1/auth/local/login",
                              json={"username": "api_jwt_check", "password": "securepass99"})
        token = r.json()["access_token"]
        settings = get_settings()
        claims = _pyjwt.decode(token, settings.system_jwt_secret, algorithms=["HS256"])
        assert claims["role"] == "system_admin"


@pytest.mark.slow
class TestApiCreateUser:
    async def test_system_admin_can_create_user(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "create_admin1", "adminpass99")
        await db_session.commit()
        # log in to get a real token with the actual user_id
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "create_admin1", "password": "adminpass99"})
        token = r_login.json()["access_token"]
        r = await client.post("/api/v1/auth/local/users",
                              json={"username": "new_op_1", "password": "initpass12",
                                    "tenant_id": 10, "role": "tenant_operator"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 201
        body = r.json()
        assert body["username"] == "new_op_1"
        assert body["role"] == "tenant_operator"

    async def test_non_admin_cannot_create_user(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        cred = await create_user(db_session, "plain_op", "initpass12", 10, "tenant_operator")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "plain_op", "password": "initpass12"})
        token = r_login.json()["access_token"]
        r = await client.post("/api/v1/auth/local/users",
                              json={"username": "hacker_user", "password": "initpass12",
                                    "tenant_id": 10, "role": "tenant_operator"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    async def test_unauthenticated_cannot_create_user(self, client):
        r = await client.post("/api/v1/auth/local/users",
                              json={"username": "anon_user", "password": "initpass12",
                                    "tenant_id": 10, "role": "tenant_operator"})
        assert r.status_code == 401

    async def test_duplicate_username_returns_409(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "create_admin2", "adminpass99")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "create_admin2", "password": "adminpass99"})
        token = r_login.json()["access_token"]
        r1 = await client.post("/api/v1/auth/local/users",
                               json={"username": "dup_api_user", "password": "initpass12",
                                     "tenant_id": 10, "role": "tenant_viewer"},
                               headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 201
        r2 = await client.post("/api/v1/auth/local/users",
                               json={"username": "dup_api_user", "password": "initpass12",
                                     "tenant_id": 10, "role": "tenant_viewer"},
                               headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 409

    async def test_password_too_short_returns_422(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "create_admin3", "adminpass99")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "create_admin3", "password": "adminpass99"})
        token = r_login.json()["access_token"]
        r = await client.post("/api/v1/auth/local/users",
                              json={"username": "short_pw_user", "password": "short",
                                    "tenant_id": 10, "role": "tenant_viewer"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 422


@pytest.mark.slow
class TestApiListUsers:
    async def test_admin_can_list_users(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "list_admin1", "adminpass99")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "list_admin1", "password": "adminpass99"})
        token = r_login.json()["access_token"]
        r = await client.get("/api/v1/auth/local/users",
                             headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_non_admin_cannot_list_users(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        cred = await create_user(db_session, "list_plain_op", "initpass12", 10, "tenant_operator")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "list_plain_op", "password": "initpass12"})
        token = r_login.json()["access_token"]
        r = await client.get("/api/v1/auth/local/users",
                             headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    async def test_unauthenticated_returns_401(self, client):
        r = await client.get("/api/v1/auth/local/users")
        assert r.status_code == 401

    async def test_list_includes_expected_fields(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "list_admin_fields", "adminpass99")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "list_admin_fields", "password": "adminpass99"})
        token = r_login.json()["access_token"]
        r = await client.get("/api/v1/auth/local/users",
                             headers={"Authorization": f"Bearer {token}"})
        users = r.json()
        assert len(users) >= 1
        u = users[0]
        assert "user_id" in u
        assert "username" in u
        assert "tenant_id" in u
        assert "role" in u
        assert "must_change_password" in u
        assert "created_at" in u


@pytest.mark.slow
class TestApiChangePassword:
    async def test_user_can_change_own_password(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        cred = await create_user(db_session, "chpw_api_user1", "initpass12", 10, "tenant_operator")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "chpw_api_user1", "password": "initpass12"})
        token = r_login.json()["access_token"]
        r = await client.post("/api/v1/auth/local/password",
                              json={"old_password": "initpass12", "new_password": "newpassword99"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # old password no longer works
        r2 = await client.post("/api/v1/auth/local/login",
                               json={"username": "chpw_api_user1", "password": "initpass12"})
        assert r2.status_code == 401

    async def test_wrong_old_password_returns_401(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        cred = await create_user(db_session, "chpw_api_user2", "initpass12", 10, "tenant_operator")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "chpw_api_user2", "password": "initpass12"})
        token = r_login.json()["access_token"]
        r = await client.post("/api/v1/auth/local/password",
                              json={"old_password": "wrongpass", "new_password": "newpassword99"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    async def test_new_password_too_short_returns_422(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        cred = await create_user(db_session, "chpw_api_user3", "initpass12", 10, "tenant_operator")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "chpw_api_user3", "password": "initpass12"})
        token = r_login.json()["access_token"]
        r = await client.post("/api/v1/auth/local/password",
                              json={"old_password": "initpass12", "new_password": "short"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 422

    async def test_unauthenticated_returns_401(self, client):
        r = await client.post("/api/v1/auth/local/password",
                              json={"old_password": "x", "new_password": "newpassword99"})
        assert r.status_code == 401


@pytest.mark.slow
class TestApiResetPassword:
    async def test_admin_can_reset_other_user_password(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin, create_user
        await bootstrap_admin(db_session, "reset_admin1", "adminpass99")
        target = await create_user(db_session, "reset_target1", "initpass12", 10, "tenant_viewer")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "reset_admin1", "password": "adminpass99"})
        token = r_login.json()["access_token"]
        r = await client.post(f"/api/v1/auth/local/users/{target.user_id}/reset",
                              json={"new_password": "adminresetpass99"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # new password works
        r2 = await client.post("/api/v1/auth/local/login",
                               json={"username": "reset_target1", "password": "adminresetpass99"})
        assert r2.status_code == 200

    async def test_non_admin_cannot_reset_password(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        attacker = await create_user(db_session, "reset_attacker1", "initpass12", 10, "tenant_operator")
        target2 = await create_user(db_session, "reset_target2", "initpass12", 10, "tenant_viewer")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "reset_attacker1", "password": "initpass12"})
        token = r_login.json()["access_token"]
        r = await client.post(f"/api/v1/auth/local/users/{target2.user_id}/reset",
                              json={"new_password": "hackedpass99"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    async def test_unauthenticated_cannot_reset(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import create_user
        target = await create_user(db_session, "reset_target3", "initpass12", 10, "tenant_viewer")
        await db_session.commit()
        r = await client.post(f"/api/v1/auth/local/users/{target.user_id}/reset",
                              json={"new_password": "hackedpass99"})
        assert r.status_code == 401

    async def test_nonexistent_user_id_returns_404(self, client, db_session: AsyncSession):
        from dlw.services.local_auth import bootstrap_admin
        await bootstrap_admin(db_session, "reset_admin2", "adminpass99")
        await db_session.commit()
        r_login = await client.post("/api/v1/auth/local/login",
                                    json={"username": "reset_admin2", "password": "adminpass99"})
        token = r_login.json()["access_token"]
        r = await client.post("/api/v1/auth/local/users/9999999/reset",
                              json={"new_password": "adminresetpass99"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404

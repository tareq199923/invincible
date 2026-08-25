# tests/test_accounts.py
"""Platform Phase 3 account services.

Registration/authentication, signed session cookies, project CRUD +
archive visibility, device-code lifecycle (interval, slow_down,
single-winner claims), identity links, and scoped login lockouts.
Storage-backed paths run against pg_engine like the rest of the suite.
"""
import hmac as hmac_mod
import time

import pytest

from invincible.core.accounts import (
    MIN_PASSWORD_LEN,
    SESSION_TTL,
    AccountError,
    DeviceCodeStore,
    GitHubOAuth,
    IdentityStore,
    ProjectService,
    SessionManager,
    UserService,
)
from invincible.core.identity import ApiKeyStore, LoginRateLimiter
from invincible.core.settings import settings


def _sign_with(payload: str) -> str:
    """Forge a correctly-signed payload using the current secret-derived
    key - lets tests exercise expiry without patching the clock."""
    import hashlib as _h

    secret = settings.owner_secret()
    key = _h.sha256(secret.encode()).digest()
    signature = hmac_mod.new(key, payload.encode("ascii"), _h.sha256
                             ).hexdigest()
    return f"{payload}.{signature}"


# --- registration & authentication ----------------------------------------------


@pytest.fixture
async def user_service(pg_engine):
    return UserService(pg_engine)


async def test_register_normalizes_and_authenticates(user_service):
    created = await user_service.register("  Mixed@Example.COM ", "longenough1")
    assert created["email"] == "mixed@example.com"
    assert await user_service.authenticate(
        "MIXED@example.com", "longenough1") == {
        "id": created["id"], "email": "mixed@example.com",
    }


async def test_register_rejects_bad_email_and_short_password(user_service):
    for email, password in (
        ("nope", "longenough1"),
        ("a b@example.com", "longenough1"),
        ("ok@example.com", "short"),
        ("ok@example.com", ""),
    ):
        with pytest.raises(AccountError):
            await user_service.register(email, password)


async def test_register_duplicate_email_is_explicit_409(user_service):
    await user_service.register("dup@example.com", "longenough1")
    with pytest.raises(AccountError) as excinfo:
        await user_service.register("dup@example.com", "otherpassword")
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "duplicate_email"


async def test_authenticate_is_indistinguishable_on_failure(user_service):
    await user_service.register("known@example.com", "longenough1")
    assert await user_service.authenticate("ghost@example.com", "whatever") is None
    assert await user_service.authenticate("known@example.com", "wrongpass1") is None


# --- session cookies -------------------------------------------------------------


def test_session_cookie_roundtrip(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_OWNER_SECRET", "k1")
    cookie = SessionManager.create(42)
    assert cookie.startswith("v1.42.")
    assert SessionManager.verify(cookie) == 42


def test_session_cookie_tamper_and_garbage(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_OWNER_SECRET", "k1")
    cookie = SessionManager.create(42)
    # flipping one signature hex char invalidates it
    last = cookie[-1]
    tampered = cookie[:-1] + ("0" if last != "0" else "1")
    assert SessionManager.verify(tampered) is None
    assert SessionManager.verify(None) is None
    assert SessionManager.verify("") is None
    assert SessionManager.verify("v1.abc.def.ghi") is None


def test_session_cookie_expiry(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_OWNER_SECRET", "k1")
    stale = _sign_with(f"v1.7.{int(time.time()) - 10}")
    assert SessionManager.verify(stale) is None
    fresh = _sign_with(f"v1.7.{int(time.time()) + SESSION_TTL - 10}")
    assert SessionManager.verify(fresh) == 7


def test_session_fail_closed_without_secret(monkeypatch):
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    assert SessionManager.available() is False
    assert SessionManager.verify("v1.1.9999999999.deadbeef") is None
    with pytest.raises(AccountError) as excinfo:
        SessionManager.create(1)
    assert excinfo.value.status_code == 503


# --- projects ---------------------------------------------------------------------


async def _seed_user(pg_engine, email: str) -> int:
    from invincible.core.db import users

    async with pg_engine.begin() as conn:
        return int((await conn.execute(
            users.insert()
            .values(email=email, password_hash=None, created_at=time.time())
            .returning(users.c.id)
        )).scalar_one())


@pytest.fixture
async def projects_svc(pg_engine):
    return ProjectService(pg_engine)


async def test_project_create_list_rename_archive(pg_engine, projects_svc):
    user_id = await _seed_user(pg_engine, "p1@example.com")
    made = await projects_svc.create(user_id, "Client Work")
    assert made["name"] == "Client Work"
    renamed = await projects_svc.rename(user_id, made["id"], "client work")
    assert renamed["name"] == "client work"
    listing = await projects_svc.list(user_id)
    assert [p["name"] for p in listing] == ["client work"]
    await projects_svc.archive(user_id, made["id"])
    assert await projects_svc.list(user_id) == []
    archived = await projects_svc.list(user_id, include_archived=True)
    assert len(archived) == 1 and archived[0]["archived_at"] is not None


async def test_project_validation_and_conflicts(pg_engine, projects_svc):
    user_id = await _seed_user(pg_engine, "p2@example.com")
    with pytest.raises(AccountError):
        await projects_svc.create(user_id, "")
    with pytest.raises(AccountError):
        await projects_svc.create(user_id, "x" * 101)
    made = await projects_svc.create(user_id, "dupe")
    with pytest.raises(AccountError) as excinfo:
        await projects_svc.create(user_id, "dupe")
    assert excinfo.value.status_code == 409
    with pytest.raises(AccountError):
        await projects_svc.rename(user_id, made["id"], "")


async def test_project_cross_user_is_not_found(pg_engine, projects_svc):
    owner = await _seed_user(pg_engine, "owner@example.com")
    outsider = await _seed_user(pg_engine, "outsider@example.com")
    made = await projects_svc.create(owner, "secret")
    for operation in (
        lambda: projects_svc.rename(outsider, made["id"], "steal"),
        lambda: projects_svc.archive(outsider, made["id"]),
    ):
        with pytest.raises(AccountError) as excinfo:
            await operation()
        assert excinfo.value.status_code == 404
    assert await projects_svc.list(outsider) == []


async def test_default_project_cannot_be_archived(pg_engine, projects_svc):
    from invincible.core.db import ensure_local_owner

    user_id, _ = await ensure_local_owner(pg_engine)
    default_id = next(
        p["id"] for p in await projects_svc.list(user_id) if p["is_default"]
    )
    with pytest.raises(AccountError) as excinfo:
        await projects_svc.archive(user_id, default_id)
    assert excinfo.value.code == "default_project"


# --- device codes ------------------------------------------------------------------


@pytest.fixture
async def devices(pg_engine):
    store = DeviceCodeStore(pg_engine, api_keys=ApiKeyStore(pg_engine))
    request = await store.create(interval=0)
    return store, request


async def test_device_code_shape(devices):
    _, request = devices
    assert request["user_code"].isalnum() and len(request["user_code"]) == 8
    assert len(request["device_code"]) > 30
    assert request["expires_in"] > 0


async def test_device_poll_pending_then_approve_claims_once(devices):
    store, request = devices
    result = await store.poll(request["device_code"])
    assert result["status"] == "pending"

    from invincible.core.db import users

    async with store.engine.begin() as conn:
        user_id = (await conn.execute(
            users.insert().values(email="dev@example.com",
                                  password_hash=None,
                                  created_at=time.time())
            .returning(users.c.id)
        )).scalar_one()
    assert await store.approve(request["user_code"], int(user_id)) is True

    claimed = await store.poll(request["device_code"])
    assert claimed["status"] == "complete"
    raw_key = claimed["api_key"]["raw"]
    assert raw_key.startswith("inv_")
    resolved = await store.api_keys.resolve(raw_key)
    assert resolved == {"id": claimed["api_key"]["id"], "user_id": user_id}

    # single winner: the second poll finds nothing left to claim
    with pytest.raises(AccountError) as excinfo:
        await store.poll(request["device_code"])
    assert excinfo.value.code == "expired_token"


async def test_device_deny(devices):
    store, request = devices
    first = await store.poll(request["device_code"])
    assert first["status"] == "pending"
    assert await store.deny(request["user_code"]) is True
    with pytest.raises(AccountError) as excinfo:
        await store.poll(request["device_code"])
    assert excinfo.value.code == "access_denied"


async def test_device_slow_down_enforced(pg_engine):
    store = DeviceCodeStore(pg_engine, api_keys=ApiKeyStore(pg_engine))
    request = await store.create(interval=600)
    first = await store.poll(request["device_code"])
    assert first["status"] == "pending"
    with pytest.raises(AccountError) as second:
        await store.poll(request["device_code"])
    assert second.value.code == "slow_down"
    assert second.value.extra["interval"] >= 600


async def test_device_unknown_and_expired(pg_engine):
    store = DeviceCodeStore(pg_engine, api_keys=ApiKeyStore(pg_engine))
    with pytest.raises(AccountError) as unknown:
        await store.poll("no-such-device-code")
    assert unknown.value.code == "expired_token"

    request = await store.create(ttl=-1)  # already expired at creation
    with pytest.raises(AccountError) as expired:
        await store.poll(request["device_code"])
    assert expired.value.code == "expired_token"


async def test_device_user_code_unique_constraint(pg_engine):
    """The DB enforces user_code uniqueness; the create() retry loop only
    has to survive astronomically unlikely randomness, so the constraint
    itself is what the test pins."""
    import sqlalchemy as sa

    from invincible.core.db import device_codes

    store = DeviceCodeStore(pg_engine, api_keys=ApiKeyStore(pg_engine))
    request = await store.create()
    async with pg_engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            await conn.execute(
                device_codes.insert().values(
                    device_code_hash="another-hash",
                    user_code=request["user_code"],
                    created_at=time.time(),
                    expires_at=time.time() + 60,
                )
            )


# --- identities ---------------------------------------------------------------------


async def test_identity_link_idempotent_and_lookup(pg_engine):
    identities = IdentityStore(pg_engine)
    alice = await _seed_user(pg_engine, "alice@example.com")
    bob = await _seed_user(pg_engine, "bob@example.com")
    link = await identities.link(alice, "github", "12345")
    again = await identities.link(alice, "github", "12345")
    assert link["id"] == again["id"]
    assert await identities.get_user("github", "12345") == alice
    assert await identities.get_user("github", "other") is None
    # same provider id under another provider never collides
    await identities.link(bob, "gitlab", "12345")
    assert await identities.get_user("gitlab", "12345") == bob


# --- scoped login lockouts ---------------------------------------------------------


async def test_lockout_scopes_are_independent(pg_engine):
    owner_limiter = LoginRateLimiter(pg_engine, scope="owner")
    login_limiter = LoginRateLimiter(pg_engine, scope="auth-login")
    for _ in range(5):
        await login_limiter.record_failure("10.0.0.9")
    assert await login_limiter.locked_out("10.0.0.9") is not None
    # hammering /auth/login never locks the owner-consent form
    assert await owner_limiter.locked_out("10.0.0.9") is None
    await login_limiter.reset("10.0.0.9")
    assert await login_limiter.locked_out("10.0.0.9") is None


async def test_legacy_owner_scope_rows_shape(pg_engine):
    limiter = LoginRateLimiter(pg_engine)
    assert limiter.scope == "owner"
    await limiter.record_failure("10.0.0.10")
    assert await limiter.locked_out("10.0.0.10") is None
    for _ in range(limiter.max_attempts - 1):
        await limiter.record_failure("10.0.0.10")
    assert await limiter.locked_out("10.0.0.10") is not None


# --- github client (hermetic, MockTransport) ----------------------------------------


def _github_transport(handler):
    import httpx

    return httpx.MockTransport(handler)


async def test_github_exchange_profile_and_email():
    seen = {}

    def handler(request: "httpx.Request") -> "httpx.Response":
        seen["authz"] = request.headers.get("Authorization")
        if request.url.host == "github.com":
            assert request.headers["Accept"] == "application/json"
            import json

            body = json.loads(request.read().decode())
            assert body["code"] == "good-code"
            assert body["client_secret"] == "shh"
            assert body["client_id"] == "cid"
            return httpx.Response(200, json={"access_token": "gho_t"})
        if request.url.path == "/user":
            assert seen["authz"] == "Bearer gho_t"
            return httpx.Response(200, json={"id": 555, "login": "octcat",
                                             "email": None})
        if request.url.path == "/user/emails":
            return httpx.Response(200, json=[
                {"email": "alt@example.com", "primary": False,
                 "verified": True},
                {"email": "main@example.com", "primary": True,
                 "verified": True},
                {"email": "unverified@example.com", "primary": False,
                 "verified": False},
            ])
        return httpx.Response(500)

    import httpx

    gh = GitHubOAuth("cid", "shh", transport=_github_transport(handler))
    try:
        token = await gh.exchange_code("good-code", "http://cb")
        assert token == "gho_t"
        profile = await gh.fetch_profile(token)
        assert profile["id"] == "555"
        email = await gh.primary_verified_email(token)
        assert email == "main@example.com"
    finally:
        await gh.aclose()


async def test_github_primary_falls_back_to_any_verified():
    import httpx

    def handler(_request: "httpx.Request") -> "httpx.Response":
        return httpx.Response(200, json=[
            {"email": "only@example.com", "primary": False,
             "verified": True},
        ])

    gh = GitHubOAuth("cid", "shh", transport=_github_transport(handler))
    try:
        assert await gh.primary_verified_email("t") == "only@example.com"
    finally:
        await gh.aclose()


async def test_github_no_verified_email_returns_none():
    import httpx

    def handler(_request: "httpx.Request") -> "httpx.Response":
        return httpx.Response(200, json=[
            {"email": "shady@example.com", "primary": True,
             "verified": False},
        ])

    gh = GitHubOAuth("cid", "shh", transport=_github_transport(handler))
    try:
        assert await gh.primary_verified_email("t") is None
    finally:
        await gh.aclose()


def test_github_authorize_url_shape():
    import httpx

    gh = GitHubOAuth("cid", "shh",
                     transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    try:
        url = gh.build_authorize_url(
            "st4te", "http://localhost:8000/auth/github/callback")
        assert url.startswith(GitHubOAuth.AUTHORIZE_URL)
        assert "state=st4te" in url
        assert "client_id=cid" in url
        assert "scope=read%3Auser+user%3Aemail" in url
    finally:
        pass


def test_min_password_constant_matches_docs():
    assert MIN_PASSWORD_LEN == 8

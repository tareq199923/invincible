# tests/test_oauth_consent_relaxation.py
"""Phase 10 coupling: the operator-only consent gate relaxes for plain
users IFF agent routing is on, because with routing on a confirmed tool
never executes on the server host - approving exposes only the user's
own machine. Routing off (the default): the Phase 5/6 refusal stands
unchanged. The coupling must never be "simplified" apart - relaxing the
gate without routing reopens the self-registered-session -> host-shell
escalation for every user.
"""
from urllib.parse import parse_qs, urlparse

from sqlalchemy import text

from invincible.main import app
from tests.conftest import oauth_exchange
from tests.test_oauth_consent_subject import (
    _flow_as_session_user,
    _scalar,
    _seed_prior_human,
)


async def test_routing_off_plain_user_still_refused(client, monkeypatch):
    monkeypatch.delenv("INVINCIBLE_AGENT_ROUTING", raising=False)
    await _seed_prior_human()
    _, params, _, _, _ = await _flow_as_session_user(
        client, "relax-off@example.com", promote=False)

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 403
    assert await _scalar("SELECT COUNT(*) FROM oauth_codes") == 0


async def test_routing_on_plain_user_can_approve(client, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    await _seed_prior_human()
    uid, params, verifier, client_id, redirect_uri = (
        await _flow_as_session_user(
            client, "relax-on@example.com", promote=False))

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 302, approved.text[:300]
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    exchange = await oauth_exchange(
        client, code, client_id, redirect_uri, verifier)
    assert exchange.status_code == 200, exchange.text
    assert exchange.json()["access_token"]


async def test_routing_on_refusal_audit_absent_for_allowed_user(client,
                                                                 monkeypatch):
    """Relaxed approval must not write a consent_forbidden row."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    await _seed_prior_human()
    uid, params, _, _, _ = await _flow_as_session_user(
        client, "relax-audit@example.com", promote=False)
    await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    async with app.state.engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action = 'oauth.consent_forbidden' "
            "AND actor_user_id = :uid"),
            {"uid": uid},
        )).scalar()
    assert rows == 0


async def test_routing_on_but_operator_still_unaffected(client, monkeypatch):
    """Operator accounts were never gated; routing must not change
    their flow either way."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    _, params, _, _, _ = await _flow_as_session_user(
        client, "relax-op@example.com", promote=True)
    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 302

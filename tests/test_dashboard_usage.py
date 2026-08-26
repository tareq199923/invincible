# tests/test_dashboard_usage.py
"""Phase 5 PR-5D: usage aggregation (RunStore.usage_summary + views).

Covers exact aggregation math across two guaranteed-distinct UTC days,
provider/model grouping with NULL token rows, the days window +
clamping, cross-user isolation, empty states, and the overview Tokens
(7d) card. Cookie realm throughout (locked decision).
"""
import re
from datetime import datetime, timezone

from invincible.main import app
from tests.conftest import register_account


def card_count(html: str, name: str) -> int:
    match = re.search(rf'data-card="{name}"><span class="num">(\d+)<', html)
    assert match is not None, f"card {name} missing from page"
    return int(match.group(1))


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


async def make_session_pk(client, email, client_id="usage-1"):
    made, _ = await register_account(client, email)
    body = made.json()
    await app.state.sessions.append(
        client_id, [{"role": "user", "content": "hi"}],
        user_id=body["id"], project_id=body["project_id"])
    pk = await app.state.sessions.lookup(
        client_id, user_id=body["id"], project_id=body["project_id"])
    return body["id"], pk


async def seed_run(pk, request_id, provider, model, index, outcome, ts,
                   inp=None, out=None):
    await app.state.runs.record({
        "request_id": request_id, "session_id": "usage-1",
        "session_pk": pk, "provider_name": provider, "model_id": model,
        "attempt_index": index, "outcome": outcome, "error_class": None,
        "input_tokens": inp, "output_tokens": out,
        "started_at": ts, "finished_at": ts + 1.0, "meta": None,
    })


async def seed_two_day_spread(client, email):
    """Two recent runs on one UTC day, two more ~49h earlier on another;
    NULL-token rows included so sums must ignore them."""
    import time

    uid, pk = await make_session_pk(client, email)
    now = time.time()
    recent, old = now - 3600, now - (2 * 86400 + 3600)
    # Recent day: a failover pair (alpha -> gamma), real tokens on winner.
    await seed_run(pk, "req-a", "alpha", "alpha-model", 1, "failover",
                   recent, None, None)
    await seed_run(pk, "req-a", "gamma", "gamma-model", 2, "ok",
                   recent + 2, 100, 20)
    # Old day: separate clean request + an errored attempt without usage.
    await seed_run(pk, "req-b", "alpha", "alpha-model", 1, "ok",
                   old, 50, 5)
    await seed_run(pk, "req-c", "beta", "beta-model", 1, "error", old + 2)
    return uid, {"recent": utc_day(recent), "old": utc_day(old)}


# --- Anonymous gate -------------------------------------------------------


async def test_usage_surfaces_require_session(client):
    assert (await client.get("/usage")).status_code == 401
    assert (await client.get("/dashboard/usage")).status_code == 401


# --- Aggregation math -------------------------------------------------------


async def test_summary_math_across_days_providers_and_null_tokens(client):
    _, day = await seed_two_day_spread(client, "math@example.com")

    data = (await client.get("/usage", params={"days": 7})).json()
    assert data["days"] == 7
    assert data["totals"] == {
        "attempts": 4, "failovers": 2,
        "input_tokens": 150, "output_tokens": 25,
    }
    by_key = {(r["day"], r["provider_name"],
               r["model_id"]): r for r in data["rows"]}
    assert by_key[(day["recent"], "alpha", "alpha-model")] == {
        "day": day["recent"], "provider_name": "alpha",
        "model_id": "alpha-model", "attempts": 1, "failovers": 1,
        "input_tokens": 0, "output_tokens": 0,
    }
    assert by_key[(day["recent"], "gamma", "gamma-model")]["attempts"] == 1
    assert by_key[(day["old"], "beta", "beta-model")]["failovers"] == 1


async def test_window_parameter_filters_and_clamps(client):
    await seed_two_day_spread(client, "window@example.com")
    one = (await client.get("/usage", params={"days": 1})).json()
    assert one["totals"] == {"attempts": 2, "failovers": 1,
                             "input_tokens": 100, "output_tokens": 20}
    wide = (await client.get("/usage", params={"days": 30})).json()
    assert wide["totals"]["attempts"] == 4
    zero = (await client.get("/usage", params={"days": 0})).json()
    assert zero["days"] == 1          # clamped up
    huge = (await client.get("/usage", params={"days": 999})).json()
    assert huge["days"] == 90         # clamped down


async def test_usage_isolated_per_user(client):
    await seed_two_day_spread(client, "owner@example.com")
    # Switch cookie to a second account: zero everything, even though the
    # underlying runs table has rows.
    await register_account(client, "other@example.com")
    blank = (await client.get("/usage")).json()
    assert blank["totals"] == {"attempts": 0, "failovers": 0,
                               "input_tokens": 0, "output_tokens": 0}
    assert blank["rows"] == []


async def test_empty_state_renders(client):
    await register_account(client, "fresh@example.com")
    page = await client.get("/dashboard/usage")
    assert page.status_code == 200
    assert "No provider runs in this window." in page.text
    json_empty = (await client.get("/usage")).json()
    assert json_empty["rows"] == []


async def test_usage_page_renders_bars_and_provider_table(client):
    _, day = await seed_two_day_spread(client, "page@example.com")
    page = await client.get("/dashboard/usage")
    assert page.status_code == 200
    assert f'data-day="{day["recent"]}"' in page.text
    assert f'data-day="{day["old"]}"' in page.text
    assert page.text.count('class="provider-row"') == 3
    assert "<td>gamma</td>" in page.text and "gamma-model" in page.text
    assert "day buckets are UTC" in page.text


async def test_overview_tokens_card_matches_summary(client):
    await seed_two_day_spread(client, "cardy@example.com")
    page = await client.get("/dashboard")
    # 150 input + 25 output across the seeded window.
    assert card_count(page.text, "usage-7d") == 175

    await register_account(client, "cardy-other@example.com")
    theirs = await client.get("/dashboard")
    assert card_count(theirs.text, "usage-7d") == 0

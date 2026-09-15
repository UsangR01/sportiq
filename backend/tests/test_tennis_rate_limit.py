"""The ATP request budget must hold across processes, and a lost tier must stop costing budget.

Built before downgrading the ATP subscription from GOAT (600/min) to ALL-STAR (60/min). One
fixture's pre-match features were measured at 17 ATP requests; a busy week is on the order of
1,500 on the daily ingest, and the adapter gives up after five 429 retries -- so on the lower
tier, saturation makes tennis predictions fail silently rather than merely run slowly.

These tests run against the REAL Redis the app uses, not a fake: the property being bought is
coordination between separate processes, and a fake would test the fake.
"""

import asyncio
import time
import uuid

import httpx
import pytest

from app.adapters import rate_limit
from app.adapters.balldontlie_tennis import _get_with_retry


@pytest.fixture
async def namespace():
    """A throwaway namespace per test, so no test inherits another's slot or denial."""
    ns = f"test-{uuid.uuid4().hex[:8]}"
    yield ns
    async with rate_limit._redis() as redis:
        async for key in redis.scan_iter(f"balldontlie:{ns}:*"):
            await redis.delete(key)


async def test_concurrent_callers_are_spaced_at_the_configured_rate(namespace):
    """THE PROPERTY. Five callers firing at once -- standing in for the API and the worker
    competing for one subscription -- must be spread out, never let through together."""
    rpm = 300  # 200ms spacing, fast enough for a test and slow enough to measure
    stamps: list[float] = []

    async def caller():
        await rate_limit.acquire_slot(namespace, rpm)
        stamps.append(time.monotonic())

    await asyncio.gather(*(caller() for _ in range(5)))
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]

    assert len(gaps) == 4
    # Allow a little scheduler jitter below the nominal 0.2s, but nothing like a burst.
    assert min(gaps) >= 0.17, f"requests escaped the budget together: gaps {gaps}"


async def test_zero_disables_pacing(namespace):
    """How the rest of the suite runs. If this ever paces, every adapter test gets slower."""
    started = time.monotonic()
    for _ in range(20):
        await rate_limit.acquire_slot(namespace, 0)
    assert time.monotonic() - started < 0.1


async def test_redis_down_paces_locally_instead_of_crashing(namespace, monkeypatch):
    """A Redis blip must never stop tennis ingest outright."""

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _broken():
        raise ConnectionError("redis is down")
        yield  # pragma: no cover

    monkeypatch.setattr(rate_limit, "_redis", _broken)
    started = time.monotonic()
    await rate_limit.acquire_slot(namespace, 600)  # 100ms
    assert time.monotonic() - started >= 0.09


def _client(handler, tour="atp"):
    return httpx.AsyncClient(
        base_url=f"https://api.balldontlie.io/{tour}/v1", transport=httpx.MockTransport(handler)
    )


async def test_a_lost_tier_is_learned_and_stops_costing_requests(namespace, monkeypatch):
    """THE SECOND PROPERTY. After the downgrade /head_to_head can only 401, and every fixture
    screen would otherwise keep spending the worker's 60/min on it. The first 401 is remembered
    and the next call never reaches the network.

    Self-configuring rather than an environment variable, because a tier flag set by hand on the
    day of the downgrade is exactly the step that gets forgotten.
    """
    hits = {"n": 0}

    def handler(request):
        hits["n"] += 1
        return httpx.Response(401, json={"error": "upgrade required"})

    monkeypatch.setattr(
        rate_limit, "_denied_key", lambda ns, ep: f"balldontlie:{namespace}:denied:{ep}"
    )
    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _get_with_retry(client, "/head_to_head", {})
        with pytest.raises(httpx.HTTPStatusError):
            await _get_with_retry(client, "/head_to_head", {})

    assert hits["n"] == 1, "the second call must be answered from memory, not the network"


async def test_a_denial_is_per_endpoint(namespace, monkeypatch):
    """Losing GOAT's /head_to_head must not take ALL-STAR's /matches down with it -- /matches is
    what the predictions actually depend on."""
    monkeypatch.setattr(
        rate_limit, "_denied_key", lambda ns, ep: f"balldontlie:{namespace}:denied:{ep}"
    )

    def handler(request):
        if request.url.path.endswith("/head_to_head"):
            return httpx.Response(401)
        return httpx.Response(200, json={"data": []})

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _get_with_retry(client, "/head_to_head", {})
        response = await _get_with_retry(client, "/matches", {})

    assert response.status_code == 200


async def test_a_rate_limit_is_never_mistaken_for_a_lost_tier(namespace, monkeypatch):
    """A 429 says nothing about entitlement. Remembering it as one would switch off an endpoint
    for an hour because of a momentary burst."""
    monkeypatch.setattr(
        rate_limit, "_denied_key", lambda ns, ep: f"balldontlie:{namespace}:denied:{ep}"
    )
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        # Retry-After: 0 keeps the backoff instant without patching asyncio.sleep globally, which
        # would also reach the event loop's own internals.
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"data": []})

    async with _client(handler) as client:
        response = await _get_with_retry(client, "/matches", {})

    assert response.status_code == 200
    assert not await rate_limit.is_denied(namespace, "matches")


async def test_atp_and_wta_spend_separate_budgets(namespace):
    """BallDontLie bills and limits each tour as its own subscription, so pacing one must not
    throttle the other."""
    rpm = 60  # a full second of spacing would be unmistakable if the two shared a key
    await rate_limit.acquire_slot(f"{namespace}-atp", rpm)
    started = time.monotonic()
    await rate_limit.acquire_slot(f"{namespace}-wta", rpm)
    assert time.monotonic() - started < 0.2


def test_every_tennis_request_goes_through_the_paced_helper():
    """The budget only holds if nothing bypasses it. Audited when pacing was added -- this pins
    it, because a single new `client.get` elsewhere in the adapter would quietly reopen the gap.
    """
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "adapters" / "balldontlie_tennis.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "post", "request", "stream"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"client", "c", "http"}
                and func.name != "_get_with_retry"
            ):
                offenders.append(f"{func.name}:{node.lineno}")
    assert offenders == [], f"network calls outside _get_with_retry: {offenders}"


async def test_the_adapter_itself_is_paced_not_just_the_limiter(namespace, monkeypatch):
    """The other tests exercise acquire_slot directly, and the suite runs with pacing OFF -- so
    without this, deleting the one `await acquire_slot(...)` line in _get_with_retry would leave
    every test green and every production request unpaced."""
    # Patch the ONE attribute on the real settings object, not get_settings wholesale. A stub
    # carrying only the rate made the limiter's own Redis client fail to find redis_url -- which
    # the fail-open branch then swallowed into uncoordinated local sleeps, so the test "failed"
    # for a reason that had nothing to do with pacing.
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "balldontlie_tennis_requests_per_minute", 300)
    monkeypatch.setattr(rate_limit, "_slot_key", lambda ns: f"balldontlie:{namespace}:slot")
    monkeypatch.setattr(
        rate_limit, "_denied_key", lambda ns, ep: f"balldontlie:{namespace}:denied:{ep}"
    )
    stamps: list[float] = []

    def handler(request):
        stamps.append(time.monotonic())
        return httpx.Response(200, json={"data": []})

    async with _client(handler) as client:
        await asyncio.gather(*(_get_with_retry(client, "/matches", {}) for _ in range(4)))

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert min(gaps) >= 0.17, f"the adapter sent requests without waiting for a slot: {gaps}"

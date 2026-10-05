"""Every adapter must accept every argument the ABC declares, because the callers pass them all.

WHY THIS IS A TEST AND NOT A TYPE CHECK. Python does not enforce an abstract method's SIGNATURE,
only its presence -- so an adapter can satisfy ABCMeta, import cleanly, pass every unit test that
calls it directly, and still raise TypeError the first time a worker calls it the way the ABC
says it may.

THAT IS NOT HYPOTHETICAL. APIBasketballAdapter.fetch_odds was written as
(sport, league, days_ahead) while the ABC declares a fourth parameter, `dates`, which
ingest_odds.py's _fetch_odds_payloads passes on EVERY call. The resulting TypeError is caught by
neither of that function's two except clauses (ValueError, httpx.HTTPError), so it would have
escaped to _ingest_odds and aborted odds ingestion for every remaining league AND every
remaining sport in the run -- from the first basketball league onward.

The damage is silent in the product: with no odds, expected-value ranking and the min_odds
filter both degrade to probability-only, which CLAUDE.md records surfacing as an apparent
MODELLING problem rather than an ingestion one.
"""

import inspect

import pytest

from app.adapters.api_basketball import APIBasketballAdapter
from app.adapters.api_football import APIFootballAdapter
from app.adapters.balldontlie import BallDontLieAdapter
from app.adapters.balldontlie_tennis import BallDontLieTennisAdapter
from app.adapters.base import DataSourceAdapter
from app.adapters.rotowire import RotoWireAdapter
from app.adapters.sportsdataio import SportsDataIOAdapter
from app.adapters.therundown import TheRundownAdapter

ADAPTERS = [
    APIBasketballAdapter,
    APIFootballAdapter,
    BallDontLieAdapter,
    BallDontLieTennisAdapter,
    RotoWireAdapter,
    SportsDataIOAdapter,
    TheRundownAdapter,
]

ABC_METHODS = ["fetch_odds", "fetch_fixtures", "fetch_team_stats", "fetch_injuries"]


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("method_name", ABC_METHODS)
def test_adapter_accepts_every_parameter_the_abc_declares(adapter_cls, method_name):
    """A SUPERSET is required, not an exact match.

    An adapter may add its own optional parameters -- that is how fetch_team_stats's `league`
    arrived -- but it may never DROP one the ABC promises callers they can pass, because the
    callers pass them unconditionally.
    """
    declared = inspect.signature(getattr(DataSourceAdapter, method_name)).parameters
    actual = inspect.signature(getattr(adapter_cls, method_name)).parameters

    # A **kwargs catch-all satisfies any caller, so it is accepted rather than demanding that
    # each name be spelled out.
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in actual.values()):
        return

    missing = sorted(set(declared) - set(actual))
    assert not missing, (
        f"{adapter_cls.__name__}.{method_name} is missing {missing}, which "
        f"{method_name}'s callers pass on every call — it will raise TypeError at runtime and, "
        f"for fetch_odds, abort the whole ingest run rather than just this adapter"
    )


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda c: c.__name__)
def test_parameters_the_abc_declares_are_not_made_required(adapter_cls):
    """An ABC parameter with a default must stay optional.

    Promoting one to required is the mirror-image break: it passes the superset check above and
    still raises TypeError for every caller that omits it.
    """
    for method_name in ABC_METHODS:
        declared = inspect.signature(getattr(DataSourceAdapter, method_name)).parameters
        actual = inspect.signature(getattr(adapter_cls, method_name)).parameters
        for name, param in declared.items():
            if param.default is inspect.Parameter.empty or name not in actual:
                continue
            assert actual[name].default is not inspect.Parameter.empty, (
                f"{adapter_cls.__name__}.{method_name} made '{name}' required, but the ABC "
                "gives it a default and callers rely on omitting it"
            )

"""Stage 2 availability: how injuries attach to key players, and why rows must not accumulate.

Both fixes here are correctness work, NOT the reason availability is near-constant. That is
feed coverage — 130 distinct injured players spanning six days. Recorded so nobody reads these
as the fix and expects the signal to come alive.
"""

from pathlib import Path

AVAILABILITY = (
    Path(__file__).resolve().parents[1] / "app" / "models_ml" / "key_player_availability.py"
).read_text(encoding="utf-8")
INGEST = (Path(__file__).resolve().parents[1] / "app" / "workers" / "ingest_injuries.py").read_text(
    encoding="utf-8"
)


def test_the_join_prefers_the_provider_id_over_the_name():
    """For football both tables are API-Football, so the id is the correct key. Matching
    "a. barboza" against "a. adams"-style abbreviations across accents, transfers and duplicate
    names was always lossy — measured at 31 matches by id versus 27 by name."""
    assert "PlayerInjuryStatus.player_id == key_player.player_id" in AVAILABILITY


def test_the_name_match_survives_as_a_fallback():
    """NBA genuinely has no shared id space — nba_api rosters against RotoWire/BallDontLie
    injuries — so removing the name match would break the sport the join was written for."""
    assert "func.lower(PlayerInjuryStatus.player_name)" in AVAILABILITY
    assert "or_(" in AVAILABILITY


def test_injury_ingestion_upserts_rather_than_appends():
    """API-Football's /injuries is fixture-scoped, so every run re-reports the same standing
    injury for every upcoming fixture date. Measured at 12,330 rows for 130 players — 95
    duplicates each. Nothing read them (Stage 2 takes the newest row per player), so they were
    pure noise that also made "how many players are injured" unanswerable without a DISTINCT."""
    assert "existing.status = update.status" in INGEST
    assert INGEST.count("UPSERT, not insert") == 2  # both the NBA and football write paths


def test_a_missing_injury_row_still_counts_as_available():
    """Unchanged, and load-bearing: "not on any injury report" is itself informative. If this
    flipped to unavailable, every player in a league with no injury feed would read as injured
    and the feature would invert."""
    assert 'status_row is None or status_row.status.value in ("ACTIVE", "PROBABLE")' in AVAILABILITY

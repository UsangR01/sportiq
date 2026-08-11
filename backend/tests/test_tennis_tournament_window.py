"""A tournament's advertised start_date is the MAIN DRAW — qualifying is played before it.

Those qualifying matches are real, ingested, and shown in the feed, and the mismatch froze them.
Measured on 2026-08-11 against the provider's own dates:

    Cincinnati Open            advertised 08-13   first real match 08-11   2 days early
    National Bank Open         advertised 08-02   first real match 08-01   1
    Mifel Tennis Open          advertised 07-27   first real match 07-25   2
    Mubadala Citi DC Open      advertised 07-27   first real match 07-25   2
    Millennium Estoril Open    advertised 07-20   first real match 07-18   2

The consequence was a silent one-way freeze: ingest_fixtures looks 7 days ahead, so it happily
ingested Cincinnati's 24 same-day qualifying matches; ingest_live_scores looks only +/-1 day, so
the tournament fell outside ITS window and not one of those fixtures could ever be refreshed.
Nothing errored — no request was ever made for them. Payload coverage measured 108 -> 133 and
the overlap with our own still-scheduled fixtures 4 -> 19 once padded.
"""

from datetime import date

from app.adapters.balldontlie_tennis import (
    TOURNAMENT_WINDOW_PAD_DAYS,
    _tournament_overlaps_window,
)

# The real record that exposed this, verbatim from the provider.
CINCINNATI = {"start_date": "2026-08-13", "end_date": "2026-08-23"}


def test_the_pad_clears_the_largest_observed_gap():
    """Two days was the maximum measured across five real events; three leaves margin without
    pulling in tournaments that are genuinely weeks away."""
    assert TOURNAMENT_WINDOW_PAD_DAYS >= 3


def test_a_tournament_playing_qualifying_today_is_in_window():
    """THE regression. On 2026-08-11 Cincinnati was two days from its advertised start and had
    24 matches being played, and the unpadded check excluded it."""
    today = date(2026, 8, 11)
    assert _tournament_overlaps_window(CINCINNATI, today, today) is True


def test_the_unpadded_comparison_would_have_missed_it():
    """Pins WHY the pad exists, so a future reader does not remove it as redundant: the raw
    provider dates genuinely do not contain the day those matches were played."""
    today = date(2026, 8, 11)
    t_start = date.fromisoformat(CINCINNATI["start_date"])
    t_end = date.fromisoformat(CINCINNATI["end_date"])
    assert not (t_start <= today and t_end >= today)


def test_a_tournament_still_far_away_stays_out():
    """The pad must not turn the window into 'everything'. A tournament three weeks out is
    still excluded, so each poll stays proportional to what is actually being played."""
    today = date(2026, 8, 11)
    far = {"start_date": "2026-09-01", "end_date": "2026-09-14"}
    assert _tournament_overlaps_window(far, today, today) is False


def test_a_finished_tournament_stays_in_briefly_then_drops_out():
    """Padding the end too, because a rain-delayed event can finish after its advertised close.
    A month-old tournament must still fall out."""
    today = date(2026, 8, 11)
    just_ended = {"start_date": "2026-07-28", "end_date": "2026-08-09"}
    long_gone = {"start_date": "2026-06-01", "end_date": "2026-06-14"}
    assert _tournament_overlaps_window(just_ended, today, today) is True
    assert _tournament_overlaps_window(long_gone, today, today) is False

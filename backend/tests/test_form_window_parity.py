"""The NBA form window must not exceed what serving actually fetches.

nba_features.LAST_N_FORM says how many prior matches a feature averages over. It is bounded
above by app/workers/ingest_fixtures.FEATURE_WINDOW_MATCHES, which is how many matches the
adapter FETCHES per team at serving time -- so a wider training window would average over
history the live path can never supply. The model would be trained on 15 matches and served 10,
and nothing would fail: the features would simply be computed from less data than they were
fitted on, quietly and for every prediction.

This is the same class as test_train_serve_league_parity.py (trained on leagues the app ingested
nothing for) and as the corners artefact that was byte-identical because a league contributed no
rows: work that completes, reports a number, and never reaches production intact.

Football and tennis have no equivalent bound -- both assemble from a game log that already holds
the full history -- which is exactly why the constraint is easy to forget here.
"""

from app.models_ml.nba_features import LAST_N_FORM
from app.workers.ingest_fixtures import FEATURE_WINDOW_MATCHES


def test_the_nba_form_window_fits_inside_what_serving_fetches():
    assert LAST_N_FORM <= FEATURE_WINDOW_MATCHES, (
        f"LAST_N_FORM={LAST_N_FORM} exceeds FEATURE_WINDOW_MATCHES={FEATURE_WINDOW_MATCHES}: "
        "training would average over more matches than the adapter ever fetches, so every "
        "served prediction would silently use a shorter window than the model was fitted on. "
        "Move both constants together."
    )


def test_the_window_is_a_positive_number_of_matches():
    """Guards the env override (SPORTIQ_NBA_LAST_N_FORM), which exists so a form-window
    experiment needs no code edit -- and which would otherwise accept 0 and produce features
    with no history in them at all."""
    assert LAST_N_FORM >= 1

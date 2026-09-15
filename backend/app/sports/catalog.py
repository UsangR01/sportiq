"""The leagues this product serves, in one place.

WHY IT MOVED HERE from scripts/seed_sports.py on 2026-09-15. Adding a league needs two lists to
agree -- the seed list that creates the League row, and api_football.py's LEAGUE_IDS that tells
ingest which provider competition to fetch. They are separate wirings and they HAVE drifted:
the nine Tier-1 leagues were trained into the model and seeded while LEAGUE_IDS still listed
nine, so the model had learned from leagues the app ingested nothing for and no fixture, odds or
prediction ever reached a user. Nothing failed; the work simply never arrived.

One list, imported by both the seed script and the API's own startup, plus a parity test, is the
version of that which cannot drift.

COUNTRY STRINGS ARE LOAD-BEARING, not decoration: mobile/lib/countryFlags.tsx keys off them, and
a country with no flag renders a globe with no error, no warning and no log line -- which looks
like a design choice until someone sends a screenshot. tests/test_league_flags.py crosses that
boundary on purpose, because mobile has no test runner.
"""

# (slug, display name, country)
#
# The slug is the join between this list, api_football.py's LEAGUE_IDS, therundown.py's
# _RUNDOWN_SPORT_IDS and ml/training's collection configs. It is not cosmetic.
FOOTBALL_LEAGUES: list[tuple[str, str, str]] = [
    ("epl", "Premier League", "England"),
    ("ligue1", "Ligue 1", "France"),
    ("bundesliga", "Bundesliga", "Germany"),
    ("laliga", "La Liga", "Spain"),
    ("seriea", "Serie A", "Italy"),
    ("brasileirao", "Série A", "Brazil"),
    ("scottish_prem", "Scottish Premiership", "Scotland"),
    ("championship", "Championship", "England"),
    ("ucl", "Champions League", "Europe"),
    ("uel", "Europa League", "Europe"),
    ("uecl", "Conference League", "Europe"),
    ("mls", "Major League Soccer", "USA"),
    ("csl", "Chinese Super League", "China"),
    # The nine Tier-1 leagues pooled into the trained model (train_football.py's LEAGUES).
    # Seeded so ingestion has a League row to attach fixtures to; TheRundown covers only the
    # J1 League of these, so the rest get their odds from API-Football alone, which is the
    # same graceful per-adapter fallback Brasileirão already relies on.
    ("allsvenskan", "Allsvenskan", "Sweden"),
    ("eliteserien", "Eliteserien", "Norway"),
    ("veikkausliiga", "Veikkausliiga", "Finland"),
    ("ekstraklasa", "Ekstraklasa", "Poland"),
    ("denmark_superliga", "Superliga", "Denmark"),
    ("liga_i", "Liga I", "Romania"),
    ("j1_league", "J1 League", "Japan"),
    ("czech_first", "Czech First League", "Czech-Republic"),
    ("austria_bundesliga", "Bundesliga", "Austria"),
    # Six added 2026-09-15, chosen on MEASURED coverage rather than reputation. Every one was
    # confirmed live before being written down: 8-9 real bookmakers pricing 1X2, double chance,
    # goals AND corners on a genuine upcoming fixture; 11-13 seasons carrying match statistics;
    # and real xG in TheStatsAPI (10-12 of 12 sampled matches).
    #
    # All six run the ordinary European Aug-May window and API-Football labels each season by the
    # year it STARTS, so none belongs in CALENDAR_YEAR_SEASON_LEAGUES or END_YEAR_SEASON_LEAGUES.
    # Checked per league rather than inherited -- that assumption is exactly what Brasileirão and
    # the J1 League each broke.
    #
    # TheRundown carries NONE of them (confirmed against its own /sports list), so their odds
    # come from API-Football alone.
    #
    # Rejected candidates, recorded so they are not re-proposed: Ligue 2 (no xG at all, 0 of 12
    # sampled), Spain's Segunda (absent from TheStatsAPI), K League 1 (6 bookmakers, no corners
    # market), the J2 League and Norway's 1. Division (ZERO seasons carrying statistics),
    # Australia's A-League (out of season until October) and Ireland's Premier Division (only
    # four seasons of statistics).
    ("eredivisie", "Eredivisie", "Netherlands"),
    ("primeira_liga", "Primeira Liga", "Portugal"),
    ("belgian_pro", "Pro League", "Belgium"),
    ("super_lig", "Süper Lig", "Turkey"),
    ("bundesliga_2", "2. Bundesliga", "Germany"),
    ("serie_b", "Serie B", "Italy"),
]

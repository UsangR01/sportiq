# SportIQ — exported training data

Everything needed to rebuild the football, basketball and tennis models outside the project.
Open `SportIQ_Models.ipynb` in the same folder.

## Football  (8,718 training examples once assembled)

| File | Shape | What |
|---|---|---|
| `football_game_log_<league>.parquet` | 2 rows per fixture | SEASON, FIXTURE_ID, GAME_DATE, TEAM_ID, OPPONENT_ID, HOME_AWAY, GF, GA, WDL |
| `football_xg_<league>.parquet` | 2 rows per fixture | FIXTURE_ID, TEAM_ID, XG_FOR — real expected goals (TheStatsAPI) |
| `football_corners_<league>.parquet` | 2 rows per fixture | FIXTURE_ID, TEAM_ID, CORNERS |
| `football_lineups_<league>.parquet` | 1 row per appearance | FIXTURE_ID, TEAM_ID, PLAYER_NAME — who actually played |
| `football_team_codes_<league>.parquet` | 1 row per team | TEAM_ID → short code, for cross-provider odds matching |
| `football_odds_sample_epl.parquet` | sparse | Real bookmaker prices. EPL only, ~0.7% of fixtures |

Leagues: epl, brasileirao, mls, csl, scottish_prem.

xG coverage starts at different seasons per league — EPL 2022, MLS/CSL 2023, Scottish Prem
25/26 only. Earlier seasons genuinely have none upstream; they merge as NaN, which XGBoost
treats as missing.

## Basketball (NBA)

| File | Shape | What |
|---|---|---|
| `nba_game_log.parquet` | ~14,500 rows | 6 seasons of team game logs (nba_api) |
| `nba_player_game_log.parquet` | ~154,000 rows | Player box scores — used for key-player labels |
| `nba_odds_sample.parquet` | sparse | Bounded real odds sample |

## Tennis (ATP)

| File | Shape | What |
|---|---|---|
| `tennis_game_log_atp.parquet` | 35,482 rows | One row per player per completed match, 2021-2025 |
| `tennis_rank_points_atp.parquet` | 17,008 rows | Point-in-time ranking points by player and week |

## Shared

| File | What |
|---|---|
| `team_key_players.parquet` | Stage 1 of the Big3/Top5 feature — each team's top players per season, by rank metric. Exported from Postgres. |

## Two things to know before trusting a rerun

**Leakage.** Every rolling statistic must be filtered to `GAME_DATE < fixture date`. The
notebook does this, but if you modify the feature code, that strict inequality is the guard.

**Key players have two different definitions on purpose.** The training LABEL uses box-score
presence (who actually played — knowable only afterwards). Live serving uses injury status
(knowable beforehand). They must never share code; the notebook uses the training-label form,
which is correct for backtesting and wrong for forecasting.

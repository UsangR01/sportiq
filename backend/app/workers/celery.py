from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "sportiq",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.ingest_odds",
        "app.workers.ingest_fixtures",
        "app.workers.ingest_live_scores",
        "app.workers.ingest_injuries",
        "app.workers.run_predictions",
        "app.workers.notify_users",
    ],
)

celery_app.conf.timezone = "UTC"

# run_predictions and notify_users are triggered by other tasks (new odds/injury data, late
# injury re-inference) rather than run on a fixed cadence — not scheduled here (TDD §2.3/§5.4).
celery_app.conf.beat_schedule = {
    "ingest-odds-every-5-minutes": {
        "task": "app.workers.ingest_odds.ingest_odds",
        "schedule": 300.0,
    },
    "ingest-live-scores-every-5-minutes": {
        "task": "app.workers.ingest_live_scores.ingest_live_scores",
        "schedule": 300.0,
    },
    "ingest-fixtures-daily": {
        "task": "app.workers.ingest_fixtures.ingest_fixtures",
        "schedule": crontab(hour=2, minute=0),
    },
    "ingest-injuries-every-30-minutes": {
        "task": "app.workers.ingest_injuries.ingest_injuries",
        "schedule": 1800.0,
    },
}

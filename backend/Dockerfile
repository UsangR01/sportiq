# Built from the REPO ROOT, not backend/ — the image needs ml/artifacts/deployed/ and a build
# context cannot reach outside itself. infra/render.yaml sets dockerContext accordingly.
#     docker build -f backend/Dockerfile -t sportiq-api .
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a code-only change does not reinstall them.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

# The artefacts models_registry currently marks active (~2.6MB), staged by
# backend/scripts/stage_artefacts.py. app/models_ml/base.py resolves a registry row's filename
# against MODELS_DIR, so these two lines are the whole of what makes a registry row loadable in
# a container — without them a portable filename resolves to a file that is not there.
COPY ml/artifacts/deployed/ /app/models/
ENV MODELS_DIR=/app/models

# The multi-season football game logs (~0.5MB, all 18 leagues). Retrodiction rebuilds a past
# fixture's features from these; without them the container falls back to the ~7 days of
# fixtures in its own database, which is far too little to compute a rolling stat for most
# teams. Measured in production before this line existed: every retrodicted prediction came out
# as the model's flat prior (H0.454 D0.294 A0.252 on every fixture in every league), those
# correctly failed MIN_EDGE_OVER_BASE_RATE because they said nothing, and 97 finished cards
# showed no pick at all.
#
# Game logs ONLY. The lineup parquets are much larger and feed the key-player features, which
# d0a24d9 pruned from the model vector -- confirmed against FEATURE_NAMES, not assumed.
COPY ml/data/football_game_log_*.parquet /app/ml_data/
# The resolved xG/shot-quality frames (~200KB total) ride along for the same reason: the
# season-opener fallback builds an upcoming fixture's features from this history, and a
# container without them silently loses the xG features that training had.
COPY ml/data/football_xg_*.parquet /app/ml_data/
ENV FOOTBALL_DATA_DIR=/app/ml_data

# One image, three roles (web / worker / beat); render.yaml overrides the command per service.
# Beat is easy to leave out, and this project has already lost hours to exactly that — four
# workers running and no beat, so nothing executed on the schedule at all.
# SHELL form, not the JSON array form, and that is the whole point: exec form does NOT expand
# environment variables, so "0.0.0.0:${PORT}" would be passed to gunicorn as that literal string.
#
# Render assigns the port it routes to via $PORT and health-checks THAT port. A hardcoded 8000
# means the container comes up healthy inside itself while Render sees nothing listening where
# it looked. The fallback keeps `docker run -p 8000:8000` working locally, where PORT is unset.
#
# `exec` is load-bearing too. Shell form runs the command under /bin/sh, which stays PID 1 and
# does NOT forward signals — so Render's SIGTERM on every deploy or scale-down would hit the
# shell while gunicorn kept serving until it was killed outright, cutting in-flight requests
# instead of draining them. exec replaces the shell, so gunicorn IS PID 1 and shuts down
# gracefully. (This is what Docker's JSONArgsRecommended warning is really about.)
#
# WORKER COUNT COMES FROM THE PLATFORM, and it used to be a hardcoded `-w 2` that silently
# overrode it. Render logs "Setting WEB_CONCURRENCY=1 by default, based on available CPUs" on
# every boot — a starter instance has half a CPU — and gunicorn then booted TWO workers anyway,
# because the flag wins over the environment variable.
#
# Two workers means TWO FULL COPIES of the app: ~128MB each on a 512MB instance, plus the
# master, plus a SQLAlchemy pool apiece, plus whatever a request allocates. That left almost no
# headroom, and the symptom was not an error — the container was killed and restarted with no
# traceback at all, nine times in twenty minutes, every boot logging a clean "Application
# startup complete" just before dying. An OOM kill looks exactly like a healthy service that
# keeps restarting.
#
# UvicornWorker is async, so one worker is not one concurrent request: it serves the whole event
# loop. The default stays 2 for local `docker run`, where no platform sets the variable.
CMD exec gunicorn -w "${WEB_CONCURRENCY:-2}" -k uvicorn.workers.UvicornWorker app.main:app --bind "0.0.0.0:${PORT:-8000}"

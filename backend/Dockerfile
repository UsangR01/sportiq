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
CMD exec gunicorn -w 2 -k uvicorn.workers.UvicornWorker app.main:app --bind "0.0.0.0:${PORT:-8000}"

# Runbook: deploying SportPIQ

Backend is three Render services off one image; mobile ships separately, usually over the air.
Nothing here is automatic — there is no `autoDeploy`, so a `git push` alone changes nothing in
production.

## The shape of it

| Service | What it does | Deploy it? |
|---|---|---|
| `sportpiq-api` | FastAPI. **Runs `alembic upgrade head` in `preDeployCommand`** | always, **first** |
| `sportpiq-worker` | Celery worker — executes ingest/inference tasks | always |
| `sportpiq-beat` | Celery beat — dispatches them on a schedule | always |
| `sportpiq-db`, `sportpiq-redis` | managed Postgres + Redis | never; they persist |

**Deploy all three, every time.** They share one image but are separate services, so shipping
only the web one leaves the worker and beat running the code they last started with. That
failure is silent: nothing errors, the API looks healthy, and stale logic quietly keeps
running. This project has lost hours to it repeatedly.

**API first, always.** Its `preDeployCommand` runs migrations exactly once, before it serves
traffic, so the worker and beat never come up against an older schema.

---

## Standard deploy

### 1. Before you start

```bash
cd backend
.venv/Scripts/python -m pytest tests/ -q
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m black --check app/ tests/
cd ../mobile && npx tsc --noEmit
```

Note the fingerprint you are replacing, so step 4 can prove the deploy landed:

```bash
curl -s https://sportpiq-api.onrender.com/health
```

And check whether this deploy carries a **migration** — if `backend/alembic/versions/` has a
file newer than the last deploy, it does, and the ordering below matters more than usual.

### 2. Deploy `sportpiq-api`

Render dashboard → `sportpiq-api` → **Manual Deploy** → *Deploy latest commit*.

Wait for green. If the pre-deploy step fails, **the migration failed** — read that log before
retrying, because a half-applied migration is worth understanding rather than re-running.

### 3. Deploy `sportpiq-worker`, then `sportpiq-beat`

Same action on each. Beat last, so the scheduler starts against code the worker already has.

### 4. Verify — do not assume

**From your own machine, or a browser** — not the Render shell (the container image is slim and
has **no `curl`**):

```bash
curl -s https://sportpiq-api.onrender.com/health
```

- `code.loaded.fingerprint` must have **changed** from what you noted in step 1.
- `code.stale` must be `false`.

A fingerprint that has not moved means the deploy did not take, whatever the dashboard says.
Note this endpoint does **not** touch the database, so a 200 says nothing about Postgres.

**From inside the Render shell**, ask the image directly which code it holds — same function
`/health` uses, no HTTP involved:

```bash
PYTHONPATH=. python -c "from app.core.code_version import source_fingerprint; print(source_fingerprint())"
```

Then spot-check something the deploy actually changed — a real endpoint, not just `/health`.

---

## When a deploy needs more than a deploy

### A new model was trained

Artefacts are baked into the image, so promoting a **new** model is not a DB update alone:

```bash
cd backend
PYTHONPATH=. python scripts/stage_artefacts.py            # dry run
PYTHONPATH=. python scripts/stage_artefacts.py --confirm
git add ml/artifacts/deployed && git commit && git push
```

Then deploy, and **in the Render shell for `sportpiq-api`**:

```bash
PYTHONPATH=. python scripts/seed_model_registry.py --confirm
```

**Order is load-bearing: deploy → `seed_model_registry` → everything else.** Running the other
seeds first once left production pointing at an artefact that was no longer in the image, and
every prediction failed with `FileNotFoundError`. A registry with no active row for a sport is
equally silent — that sport simply stops producing picks, with nothing in the logs.

### A new league or sport was added

In the Render shell for `sportpiq-api`, after deploying:

```bash
PYTHONPATH=. python scripts/seed_sports.py
PYTHONPATH=. python scripts/seed_elo_from_game_log.py --confirm
```

**The Elo trap, hit twice:** ingest settles completed rounds before you seed, which gives those
teams a 1–2 match rating, and the seeder skips non-NULL ratings. Clear the affected league's
ratings first, then seed — scoped to that league so domestic clubs keep theirs.

### Nothing seems to be running

```bash
cd backend && python scripts/check_stale.py     # exit 0 = current, 1 = something is stale
```

It asks every long-lived process which code it loaded — API, worker and beat at once — and
exits non-zero if any is stale, so it can gate a verification step rather than being read by
eye. A stale **worker** applies old logic to work it receives; a stale **beat** never dispatches
the work at all, which is quieter and worse.

---

## Mobile

Two routes, and picking the wrong one wastes an hour.

### JS/asset-only change → EAS Update (minutes)

```bash
cd mobile
npx eas-cli update --channel preview --environment preview \
  --message "what changed" --non-interactive
```

**`--environment preview` is not optional.** `eas update` reads EAS's **server-side**
environment, *not* `eas.json`'s build-profile `env` block. Miss it and the bundle ships with no
`EXPO_PUBLIC_API_URL`, falls back to `localhost:8000`, and the app cannot reach anything —
this shipped once and looked like a total outage.

On the device: **fully close and reopen twice.** `expo-updates` downloads on one launch and
applies on the next. Pull-to-refresh does not fetch updates.

### Native change → EAS Build (much slower)

A new native dependency, an `app.json`/`app.config` change, or a runtime-version bump needs a
real build. An OTA update cannot deliver those.

### Deploy the backend FIRST when mobile depends on new API fields

Mobile degrades gracefully — absent fields render as nothing rather than crashing — but the
feature is invisible until the backend is up, which reads as broken.

---

## Rolling back

Render keeps previous deploys: dashboard → service → **Rollback**. Do all three services, or
you have reintroduced the split-version problem you were escaping.

**A migration does not roll back with the code.** Every migration in this project has a real
`downgrade()`, but rolling one back is a deliberate act in the Render shell, not part of the
dashboard rollback. Prefer rolling forward with a fix.

For a **model**, rollback is a DB update and needs no deploy at all, provided the artefact is
still staged in the image:

```bash
PYTHONPATH=. python scripts/activate_model.py <version> --confirm
```

---

## Traps this project has actually hit

- **Half-deployed trio.** Web green, worker and beat on old code, everything looks fine.
- **Empty log ≠ dead process.** Output buffers. Check for the process, not for output.
- **The web shell shares the API container.** Analysis scripts have been OOM-killed twice there
  — the shell blanks and reconnects. Batch the work; `measure_pick_flips.py` documents the shape.
- **Bracketed paste.** Some shells prepend `^[[200~` to a pasted line. Type it instead.
- **No `curl` in the container.** The image is slim; `curl`-based checks belong on your own
  machine. Inside the shell, use the `source_fingerprint()` one-liner above.
- **`seed_model_registry` before other seeds**, or predictions fail on a missing artefact.
- **A stale beat is invisible.** No errors, no failed tasks — the work is simply never dispatched.

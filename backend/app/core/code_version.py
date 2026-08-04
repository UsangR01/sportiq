"""Identify which version of the code a running process actually loaded.

Long-lived processes here — the Celery worker, beat, and uvicorn — hold whatever they imported
at launch. When they fall behind the files on disk they do not fail; they keep succeeding with
old logic, which is the hardest kind of wrong to notice. In a single day that produced a fixed
kickoff-refresh that looked broken for eight hours, corrected scores silently overwritten every
five minutes, and an injury fix that appeared not to work.

WHY NOT JUST A GIT SHA

A commit SHA is the obvious answer and it is not sufficient in development, where the failure
mode is editing WITHOUT committing: the SHA is identical before and after the change that the
worker is missing. So the primary signal is a fingerprint of the source files themselves —
path, size and mtime for every .py under app/ — which moves the moment a file is saved.

The SHA is still recorded because it is the useful identifier in production, where the source
is baked into an image and there is no working tree to diverge from.

The fingerprint deliberately reads metadata rather than file contents: it runs on every
process start and on every /health call, and stat() over a few hundred files is microseconds
where hashing their contents is not. The trade-off is that a change which preserves both size
and mtime goes unnoticed — which no editor does in practice.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = APP_DIR.parent.parent


@dataclass(frozen=True)
class CodeVersion:
    """What a process is running. `fingerprint` is the one that matters in development."""

    fingerprint: str
    git_sha: str | None
    git_dirty: bool | None

    def as_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
        }


def source_fingerprint(root: Path = APP_DIR) -> str:
    """A short digest of every .py under `root`, by path/size/mtime.

    Sorted so the result is stable regardless of directory-walk order — otherwise two
    processes on identical code could report different fingerprints and every check would
    be a false alarm."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        stat = path.stat()
        digest.update(str(path.relative_to(root)).encode())
        digest.update(str(stat.st_size).encode())
        digest.update(str(int(stat.st_mtime)).encode())
    return digest.hexdigest()[:12]


def _git(*args: str) -> str | None:
    """Best-effort git read. Returns None in a container with no working tree, which is the
    normal production case rather than an error."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_DIR, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def current_code_version() -> CodeVersion:
    """Reads the CURRENT state on disk — call this to compare against what a process reported."""
    # GIT_SHA is set at image build time in production, where `git` itself is absent.
    sha = os.environ.get("GIT_SHA") or _git("rev-parse", "--short", "HEAD")
    status = _git("status", "--porcelain")
    return CodeVersion(
        fingerprint=source_fingerprint(),
        git_sha=sha,
        git_dirty=(bool(status) if status is not None else None),
    )


@lru_cache(maxsize=1)
def loaded_code_version() -> CodeVersion:
    """The version this process started with — cached on first call, so it keeps reporting
    launch-time state even as the files underneath it change. That divergence is precisely
    what makes a stale process detectable."""
    return current_code_version()

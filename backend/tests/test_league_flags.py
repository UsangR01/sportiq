"""Every seeded league's country must have a flag in the mobile app.

Adding a league is a data insert by design — no schema change, no backend code change — which
is exactly why the flag gets forgotten. It happened: the nine Tier-1 leagues shipped and
Norway, Finland, Denmark, Poland and the Czech Republic all rendered the globe fallback for
days, because a missing flag produces no error, no warning and no log line. It looks like a
design choice until someone sends a screenshot.

This test crosses the backend/mobile boundary deliberately. There is no Jest setup in mobile/
(see CLAUDE.md), so the backend suite is the only place an automated check can live, and a
cross-language check that runs is worth more than a correct one that does not exist.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_sports.py"
FLAG_MODULE = REPO_ROOT / "mobile" / "lib" / "countryFlags.tsx"


def _normalise(value: str) -> str:
    """Mirrors normaliseCountry in countryFlags.tsx — the app matches this way, so the test
    must too, or it would fail on 'Czech-Republic' while the app renders it correctly."""
    return re.sub(r"[^a-z]", "", value.lower())


def _seeded_countries() -> set[str]:
    body = SEED_SCRIPT.read_text(encoding="utf-8")
    # ("slug", "Display Name", "Country") — the third element of each league tuple.
    return {
        match.group(3)
        for match in re.finditer(r'\(\s*"([^"]+)",\s*"([^"]+)",\s*"([^"]+)"\s*\)', body)
    }


def _mapped_countries() -> set[str]:
    body = FLAG_MODULE.read_text(encoding="utf-8")
    block = body.split("COUNTRY_FLAG_IMAGES", 1)[1]
    # Keys are bare identifiers or quoted strings ("Czech-Republic").
    return {
        (m.group(1) or m.group(2))
        for m in re.finditer(r'^\s+(?:"([^"]+)"|([A-Za-z][A-Za-z0-9]*)):\s*require\(', block, re.M)
    }


def test_every_seeded_league_country_has_a_flag():
    seeded = {_normalise(c) for c in _seeded_countries()}
    mapped = {_normalise(c) for c in _mapped_countries()}
    missing = sorted(seeded - mapped)
    assert not missing, (
        f"seeded league countries with no flag in mobile/lib/countryFlags.tsx: {missing}. "
        "Add a PNG under mobile/assets/flags/ and a key to COUNTRY_FLAG_IMAGES, or the league "
        "renders a globe with nothing logged."
    )


def test_the_flag_files_actually_exist():
    """A require() pointing at a missing asset fails at bundle time, not here — but the failure
    surfaces as a broken app rather than a named file, so name it here."""
    body = FLAG_MODULE.read_text(encoding="utf-8")
    referenced = re.findall(r'require\("\.\./assets/flags/([^"]+)"\)', body)
    assert referenced, "no flag assets referenced at all — the map or this test is broken"
    missing = [
        name
        for name in referenced
        if not (REPO_ROOT / "mobile" / "assets" / "flags" / name).exists()
    ]
    assert not missing, f"referenced but absent from mobile/assets/flags/: {missing}"


def test_the_hyphenated_provider_spelling_is_handled():
    """API-Football writes 'Czech-Republic' with a hyphen while every other country is a plain
    word. Exact-key lookup silently missed it; the normalising fallback is what fixes it, and
    removing that fallback must fail here rather than in a screenshot."""
    assert "normaliseCountry" in FLAG_MODULE.read_text(encoding="utf-8")
    assert _normalise("Czech-Republic") == _normalise("Czech Republic") == "czechrepublic"

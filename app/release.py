"""
Which version this copy is, where new ones come from, and what changed.

The version lives in the VERSION file at the root of the project (one line,
like 1.2.0) and the notes in CHANGELOG.md / CHANGELOG.en.md:

    ## 1.2.0 — 2026-12-01
    - one change per line

The repository is the one install.sh was set up for (scripts/publish-github.sh
writes it below and in install.sh); UPDATE_REPO in .env overrides it.
GITHUB_TOKEN is needed only for a private repository.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "WWS909/vpn-seller"                           # set by scripts/publish-github.sh
SEMVER = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
SLUG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_HEAD = re.compile(r"^##\s+v?(\d{1,4}\.\d{1,4}\.\d{1,4})\b\s*(?:[—–-]\s*(.+))?$")


def version() -> str:
    try:
        v = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
    return v if SEMVER.match(v) else "0.0.0"


def parse(v: str) -> tuple[int, int, int]:
    if not SEMVER.match(str(v or "")):
        return (0, 0, 0)
    a, b, c = (int(x) for x in str(v).split("."))
    return (a, b, c)


def newer(a: str, b: str) -> bool:
    """Is version a later than version b?"""
    return SEMVER.match(str(a or "")) is not None and parse(a) > parse(b)


def repo() -> str:
    """owner/name of the repository, or "" when it was never set."""
    slug = (os.getenv("UPDATE_REPO") or REPO).strip()
    slug = re.sub(r"^(?:https?://)?(?:www\.)?github\.com[/:]", "", slug)
    slug = re.sub(r"^git@github\.com:", "", slug)
    slug = slug.removesuffix(".git").strip("/")
    if slug.startswith("OWNER/") or not SLUG.match(slug):
        return ""
    return slug


def branch() -> str:
    b = (os.getenv("UPDATE_BRANCH") or "main").strip()
    return b if re.match(r"^[A-Za-z0-9._/-]{1,80}$", b) else "main"


def token() -> str:
    return (os.getenv("GITHUB_TOKEN") or "").strip()


def local_notes(lang: str) -> str:
    name = "CHANGELOG.en.md" if lang == "en" else "CHANGELOG.md"
    try:
        return (ROOT / name).read_text(encoding="utf-8")[:200_000]
    except OSError:
        return ""


def notes(text: str, after: str, upto: str, limit: int = 10) -> list[dict]:
    """The sections of a changelog for versions after `after` up to and
    including `upto`, newest first: [{"version", "date", "items": [...]}]."""
    out: list[dict] = []
    cur: dict | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = _HEAD.match(line)
        if m:
            v = m.group(1)
            cur = None
            if newer(v, after) and not newer(v, upto):
                cur = {"version": v, "date": (m.group(2) or "").strip()[:40], "items": []}
                out.append(cur)
            continue
        if line.startswith("## "):          # a heading that is not a version ends the section
            cur = None
            continue
        if cur is not None and re.match(r"^[-*•]\s+", line) and len(cur["items"]) < 30:
            cur["items"].append(re.sub(r"^[-*•]\s+", "", line)[:400])
    out.sort(key=lambda s: parse(s["version"]), reverse=True)
    return out[:limit]

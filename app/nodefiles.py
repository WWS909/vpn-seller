"""
Xray for the shop's own servers, when GitHub does not answer from them.

scripts/node.sh first takes Xray from the GitHub releases. Some hosters'
servers cannot reach GitHub at times; then the installer asks the shop:
GET /node-files/Xray-linux-64.zip (and its .dgst with the SHA-256 of the
release). The shop fetches the latest release once, keeps it in
DATA_DIR/node-files and hands it out; it is fetched again after a week.
The server checks the file against the .dgst, the same as from GitHub.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path

import httpx

from .config import settings as env

RELEASES = "https://github.com/XTLS/Xray-core/releases/latest/download/"
NAMES = ("Xray-linux-64.zip", "Xray-linux-arm64-v8a.zip")
FRESH = 7 * 86400
_lock = threading.Lock()


class Unavailable(Exception):
    """Neither a kept copy nor GitHub."""


def folder() -> Path:
    return Path(env.data_dir) / "node-files"


def _get(url: str, limit: int) -> bytes:
    with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=True) as c:
        r = c.get(url)
        if r.status_code != 200:
            raise httpx.HTTPError(f"HTTP {r.status_code}")
        if len(r.content) > limit:
            raise httpx.HTTPError("too large")
        return r.content


def _fresh(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < FRESH
    except OSError:
        return False


def file_for(name: str) -> Path:
    """The path of the kept file (the zip or its .dgst), fetched now if
    there is none or it is a week old. A stale copy is still given when
    GitHub does not answer."""
    base = name[:-5] if name.endswith(".dgst") else name
    if base not in NAMES or name not in (base, base + ".dgst"):
        raise FileNotFoundError(name)
    d = folder()
    zip_path, dgst_path = d / base, d / (base + ".dgst")
    with _lock:
        if not (_fresh(zip_path) and dgst_path.is_file()):
            try:
                dgst = _get(RELEASES + base + ".dgst", 64_000)
                want = re.search(rb"SHA2-256=\s*([0-9a-fA-F]{64})", dgst)
                if not want:
                    raise httpx.HTTPError("no SHA2-256 in .dgst")
                data = _get(RELEASES + base, 200_000_000)
                if hashlib.sha256(data).hexdigest() != want.group(1).decode().lower():
                    raise httpx.HTTPError("checksum mismatch")
                d.mkdir(parents=True, exist_ok=True)
                for path, body in ((zip_path, data), (dgst_path, dgst)):
                    tmp = path.with_suffix(path.suffix + ".tmp")
                    tmp.write_bytes(body)
                    os.replace(tmp, path)
            except (httpx.HTTPError, OSError) as e:
                if not (zip_path.is_file() and dgst_path.is_file()):
                    raise Unavailable(str(e)) from e
    return dgst_path if name.endswith(".dgst") else zip_path

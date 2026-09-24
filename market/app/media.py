"""Product photos and making-clips, wherever they live.

The seed catalogue's files are in `seed/media/`. A listing an artisan
published from her phone has its photo in B2's media store instead, at a
`/media/<sha256>.jpg` URL relative to B2. Both arrive here as `/media/...`,
so this resolves a name locally first and, when running against B2,
fetches it from B2 once and keeps a copy — the shop page, the tag and the
reel renderer all need the same bytes, and a reel is built from a file on
disk, not a URL.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from app import config

#: A bare filename: no directories, no "..". B2's names are a hash plus an
#: extension and the seed's are `lst_<id>.jpg`; both fit.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def file_for(name: str) -> Path | None:
    """The local file behind `/media/<name>`, fetching it from B2 if need be."""
    if not _SAFE_NAME.match(name) or ".." in name:
        return None
    local = Path(config.MEDIA_DIR) / name
    if local.is_file():
        return local
    if config.using_stubs():
        return None

    cached = Path(config.MEDIA_CACHE_DIR) / name
    if cached.is_file():
        return cached
    try:
        response = httpx.get(f"{config.PLATFORM_URL}/media/{name}", timeout=15.0)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    cached.parent.mkdir(parents=True, exist_ok=True)
    partial = cached.with_suffix(cached.suffix + ".part")
    partial.write_bytes(response.content)
    partial.replace(cached)
    return cached

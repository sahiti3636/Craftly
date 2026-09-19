"""Where photos, making-clips and audio live.

**Content-addressed.** A file is stored under the hex SHA-256 of its own
bytes. Three consequences, all of them the reason:

1. **Re-uploading costs nothing.** An artisan on a patchy connection taps
   send, the request times out after the upload completed, and A1's
   offline queue retries. Addressing by content means the retry writes the
   same path and no duplicate appears. Addressing by upload id means her
   phone has now spent her data twice and the disk holds two identical
   photographs.
2. **A URL cannot be made to point at different bytes.** A passport is a
   proof object; a photo URL on it that can be swapped after the fact is
   not proof of anything.
3. **Deleting is not a delete.** Two listings can reference the same
   bytes, so removing a `Media` row does not remove the file. That is a
   garbage-collection job nobody needs yet, and is written down in the
   README rather than half-implemented here.

Storage is the local filesystem behind a small interface. S3 is the same
three methods and no caller changes — but object storage is a bill and a
set of credentials, and a demo that needs an AWS account to show a
photograph is a demo that does not run.

**Uploads are type-checked by sniffing the first bytes, not by trusting
the declared content type.** A client can claim anything; the magic number
at the head of the file is harder to lie about. This is not a sandbox and
it will not stop a determined attacker, but it does stop the ordinary case
of a broken client posting an HTML error page as a JPEG and a storefront
rendering a broken image for the rest of the demo.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app import config

#: Magic-number prefixes to extension + content type.
_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"RIFF", ".webp", "image/webp"),  # refined below: RIFF....WEBP
    (b"OggS", ".ogg", "audio/ogg"),
    (b"ID3", ".mp3", "audio/mpeg"),
    (b"fLaC", ".flac", "audio/flac"),
]

#: Which of those count as what, for the `kind` column.
_KIND_BY_PREFIX = {"image/": "image", "audio/": "audio", "video/": "video"}


class UnsupportedMedia(ValueError):
    """The bytes are not a media type this service will store."""


@dataclass(frozen=True)
class StoredFile:
    sha256: str
    path: Path
    extension: str
    content_type: str
    bytes: int

    @property
    def filename(self) -> str:
        return self.path.name


def sniff(data: bytes) -> tuple[str, str]:
    """Return (extension, content_type) from the leading bytes.

    Raises `UnsupportedMedia` rather than falling back to
    `application/octet-stream`: storing bytes this service cannot identify
    means serving them back later with a guessed type, and a guessed type
    on a file a browser will execute is the whole class of bug worth
    avoiding here.
    """
    head = data[:16]

    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if head[4:12] in {b"ftypisom", b"ftypmp42", b"ftypMSNV", b"ftypavc1"} or head[4:8] == b"ftyp":
        return ".mp4", "video/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm", "video/webm"

    for prefix, extension, content_type in _SIGNATURES:
        if prefix == b"RIFF":
            continue
        if head.startswith(prefix):
            return extension, content_type

    raise UnsupportedMedia(
        "That file is not an image, audio clip or video this service recognises."
    )


def kind_for(content_type: str) -> str:
    for prefix, kind in _KIND_BY_PREFIX.items():
        if content_type.startswith(prefix):
            return kind
    return "other"


class LocalMediaStore:
    """Content-addressed files on disk.

    Sharded two levels deep by the first four hex characters, because a
    single directory with fifty thousand files in it is slow to list on
    every filesystem that matters and impossible to look at by hand.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or config.MEDIA_DIR)

    def _path_for(self, digest: str, extension: str) -> Path:
        return self.root / digest[:2] / digest[2:4] / f"{digest}{extension}"

    def put(self, data: bytes) -> StoredFile:
        if not data:
            raise UnsupportedMedia("That file is empty.")
        if len(data) > config.MAX_UPLOAD_BYTES:
            raise UnsupportedMedia(
                f"That file is larger than the {config.MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit."
            )
        extension, content_type = sniff(data)
        digest = hashlib.sha256(data).hexdigest()
        path = self._path_for(digest, extension)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a sibling and rename, so a crash mid-write cannot
            # leave a truncated file sitting at a content address that
            # claims to hold the whole thing.
            temp = path.with_suffix(path.suffix + ".part")
            temp.write_bytes(data)
            temp.replace(path)
        return StoredFile(
            sha256=digest,
            path=path,
            extension=extension,
            content_type=content_type,
            bytes=len(data),
        )

    def resolve(self, filename: str) -> Path | None:
        """Map a served filename back to a path on disk.

        The filename is parsed rather than joined: a request for
        `../../etc/passwd` has no valid digest, so it resolves to None
        before it ever touches the filesystem.
        """
        stem, _, extension = filename.partition(".")
        if len(stem) != 64 or not all(c in "0123456789abcdef" for c in stem):
            return None
        path = self._path_for(stem, f".{extension}" if extension else "")
        return path if path.exists() else None

    def url_for(self, stored: StoredFile) -> str:
        return f"/media/{stored.filename}"


_store: LocalMediaStore | None = None


def store() -> LocalMediaStore:
    global _store
    if _store is None:
        _store = LocalMediaStore()
    return _store


def reset(root: Path | None = None) -> LocalMediaStore:
    """Rebind the store. Tests point it at a tmp directory with this."""
    global _store
    _store = LocalMediaStore(root)
    return _store

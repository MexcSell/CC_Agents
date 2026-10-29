"""Images attached to user messages.

Files live in UPLOAD_DIR/<conversation id>/. A stored message refers to one with the block
{"type": "image", "upload": "<cid>/<name>", "media_type": ...}; api_block() turns that into a
Messages API image block when the history is sent to the model.
"""

import base64
import binascii
import shutil
import uuid
from pathlib import Path

from . import db
from .config import DATA_DIR

UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_IMAGES = 8
# The API accepts base64 payloads up to 5 MB; the browser downscales images to stay below this.
MAX_BYTES = 3_750_000
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


class ImageError(Exception):
    pass


def _sniff(data: bytes) -> str | None:
    """The media type from the file's signature (the API rejects a type that does not match the bytes)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def save(cid: int, encoded: list[str]) -> list[dict]:
    """Decode base64 images, store them for conversation `cid` and return their message blocks."""
    if len(encoded) > MAX_IMAGES:
        raise ImageError(f"At most {MAX_IMAGES} images per message")
    files = []
    for i, item in enumerate(encoded, 1):
        try:
            data = base64.b64decode(item.split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError):
            raise ImageError(f"Image {i} is not valid base64 data")
        media_type = _sniff(data)
        if not media_type:
            raise ImageError(f"Image {i} is not a PNG, JPEG, GIF or WebP image")
        if len(data) > MAX_BYTES:
            raise ImageError(f"Image {i} is larger than {MAX_BYTES / 1e6:.2f} MB")
        files.append((data, media_type))
    folder = UPLOAD_DIR / str(cid)
    folder.mkdir(parents=True, exist_ok=True)
    blocks = []
    for data, media_type in files:
        name = f"{uuid.uuid4().hex}.{EXTENSIONS[media_type]}"
        (folder / name).write_bytes(data)
        blocks.append({"type": "image", "upload": f"{cid}/{name}", "media_type": media_type})
    return blocks


def path(ref: str) -> Path | None:
    """The file for a stored reference, or None if it is malformed or missing."""
    p = (UPLOAD_DIR / ref).resolve()
    if p.parent.parent != UPLOAD_DIR or not p.is_file():
        return None
    return p


def api_block(block: dict) -> dict:
    """A stored image reference as a Messages API image block (text if the file is gone)."""
    p = path(block.get("upload", ""))
    if p is None:
        return {"type": "text", "text": "[An image was attached here but is no longer available.]"}
    return {"type": "image", "source": {"type": "base64", "media_type": block["media_type"],
                                        "data": base64.b64encode(p.read_bytes()).decode("ascii")}}


def api_content(content):
    """Message content with stored image references expanded for the API."""
    if isinstance(content, str) or not any(b.get("type") == "image" and "upload" in b for b in content):
        return content
    return [api_block(b) if b.get("type") == "image" and "upload" in b else b for b in content]


def remove(cid: int) -> None:
    shutil.rmtree(UPLOAD_DIR / str(cid), ignore_errors=True)


def prune() -> None:
    """Delete the images of conversations that no longer exist (they also go when a topic, task or project is deleted)."""
    existing = {str(r["id"]) for r in db.query("SELECT id FROM conversations")}
    for d in UPLOAD_DIR.iterdir():
        if d.is_dir() and d.name not in existing:
            shutil.rmtree(d, ignore_errors=True)

"""Images attached to user messages.

Files live in UPLOAD_DIR/<conversation id>/. A stored message refers to one with the block
{"type": "image", "upload": "<cid>/<name>", "media_type": ...}; api_block() turns that into a
Messages API image block when the history is sent to the model.

Images attached to a task description live in TASK_DIR/<task id>/<name>; the task's `images` column
lists them as {"name", "media_type"}. They are copied into the conversation's folder when an agent starts.
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
TASK_DIR = DATA_DIR / "task_images"   # not under UPLOAD_DIR: prune() treats its folders as conversations
TASK_DIR.mkdir(parents=True, exist_ok=True)
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


def decode(encoded: list[str], limit: int = MAX_IMAGES) -> list[tuple[bytes, str]]:
    """Validate base64 images (optionally data: URLs) and return their (bytes, media type)."""
    if len(encoded) > limit:
        raise ImageError(f"At most {limit} images")
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
    return files


def _write(folder: Path, files: list[tuple[bytes, str]]) -> list[tuple[str, str]]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for data, media_type in files:
        name = f"{uuid.uuid4().hex}.{EXTENSIONS[media_type]}"
        (folder / name).write_bytes(data)
        out.append((name, media_type))
    return out


def save(cid: int, encoded: list[str]) -> list[dict]:
    """Decode base64 images, store them for conversation `cid` and return their message blocks."""
    return [{"type": "image", "upload": f"{cid}/{name}", "media_type": media_type}
            for name, media_type in _write(UPLOAD_DIR / str(cid), decode(encoded))]


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


# ---------------------------------------------------------------- task description images

def task_check(current: list[dict], encoded: list[str], removed: list[str] = ()) -> list[tuple[bytes, str]]:
    """Validate images to add to a task that has `current` (minus `removed`); returns the decoded files."""
    kept = sum(1 for i in current if i["name"] not in removed)
    files = decode(encoded, limit=len(encoded))
    if kept + len(files) > MAX_IMAGES:
        raise ImageError(f"A task can have at most {MAX_IMAGES} images")
    return files


def task_save(tid: int, files: list[tuple[bytes, str]]) -> list[dict]:
    """Store decoded images (from task_check) for task `tid`; returns the entries for its `images` column."""
    return [{"name": name, "media_type": media_type} for name, media_type in _write(TASK_DIR / str(tid), files)]


def task_path(tid: int, name: str) -> Path | None:
    """The file of a task image, or None if the name is malformed or the file is missing."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    p = TASK_DIR / str(tid) / name
    return p if p.is_file() and not p.is_symlink() else None


def task_delete(tid: int, names: list[str]) -> None:
    for name in names:
        p = task_path(tid, name)
        if p:
            p.unlink(missing_ok=True)


def task_remove(tid: int) -> None:
    shutil.rmtree(TASK_DIR / str(tid), ignore_errors=True)


def task_to_conversation(task: dict, cid: int, sent: list[str] = ()) -> list[dict]:
    """Copy the task's description images (except the names in `sent`) into conversation `cid`; returns their
    message blocks."""
    folder = UPLOAD_DIR / str(cid)
    blocks = []
    for item in task.get("images") or []:
        src = task_path(task["id"], item["name"])
        if src is None or item["name"] in sent:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, folder / item["name"])
        blocks.append({"type": "image", "upload": f"{cid}/{item['name']}", "media_type": item["media_type"]})
    return blocks


def prune() -> None:
    """Delete the images of conversations and tasks that no longer exist (they also go when a topic, task or
    project is deleted)."""
    existing = {str(r["id"]) for r in db.query("SELECT id FROM conversations")}
    for d in UPLOAD_DIR.iterdir():
        if d.is_dir() and d.name not in existing:
            shutil.rmtree(d, ignore_errors=True)
    tasks = {str(r["id"]) for r in db.query("SELECT id FROM tasks")}
    for d in TASK_DIR.iterdir():
        if d.is_dir() and d.name not in tasks:
            shutil.rmtree(d, ignore_errors=True)

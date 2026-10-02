"""Validated inline images and the per-native-session image store.

Hermes images arrive as OpenAI ``image_url`` parts holding ``data:`` URIs. They
never travel through the channel frame, which carries text only. Instead each
image is written to the native session's private runtime directory and the
native transport sees an opaque handle that the ``read_image`` MCP tool turns
back into an MCP image content block.

Only inline data URIs are accepted: no path, ``file:`` or network source is
ever opened or fetched. Handles are content addresses scoped to one native
session directory, and the store holds exactly the images present in the latest
authoritative request: whatever Hermes dropped is deleted on the next sync.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

# Conservative bridge resource budgets, not measured Claude Code/MCP limits.
# The whole HTTP request is separately capped at 8 MiB, including base64 and
# history; that transport limit can be reached before these decoded-byte caps.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 6 * 1024 * 1024
MAX_IMAGES = 64
IMAGES_DIR = "images"
HANDLE_PATTERN = re.compile(r"^i[0-9a-f]{32}$")

_PART_TYPES = frozenset({"image_url", "input_image"})
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_ALIASES = {"image/jpg": "image/jpeg"}
_DATA_URI = re.compile(r"^data:(image/[a-z]+);base64,([A-Za-z0-9+/]*={0,2})$")
_NOT_SEEN = "Image not seen by the model: "


class ImageError(ValueError):
    """An image cannot be delivered; the message always says it was not seen."""


@dataclass(frozen=True)
class DecodedImage:
    mime: str
    data: bytes

    @property
    def handle(self) -> str:
        digest = hashlib.sha256(self.mime.encode() + b"\0" + self.data)
        return "i" + digest.hexdigest()[:32]

    @property
    def filename(self) -> str:
        return f"{self.handle}.{_EXTENSIONS[self.mime]}"


def is_image_part(part) -> bool:
    return isinstance(part, dict) and part.get("type") in _PART_TYPES


def _signature_matches(mime: str, data: bytes) -> bool:
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def decode_image_part(part) -> DecodedImage:
    """Strictly decode one image part or raise ``ImageError``."""
    if not isinstance(part, dict) or part.get("type") not in _PART_TYPES:
        raise ImageError(_NOT_SEEN + "not an image content part.")
    extras = set(part) - {"type", "image_url"}
    if extras:
        raise ImageError(_NOT_SEEN + "unsupported image part fields.")
    source = part.get("image_url")
    if isinstance(source, dict):
        if set(source) - {"url", "detail"}:
            raise ImageError(_NOT_SEEN + "unsupported image_url fields.")
        source = source.get("url")
    if not isinstance(source, str):
        raise ImageError(_NOT_SEEN + "image source must be an inline data URI.")
    if not source.startswith("data:"):
        raise ImageError(
            _NOT_SEEN
            + "only inline base64 data: URIs are supported; paths, file: and "
            "remote URLs are never read or fetched."
        )
    found = _DATA_URI.match(source)
    if found is None:
        raise ImageError(
            _NOT_SEEN + "malformed data URI; expected data:image/<type>;base64,<data>."
        )
    mime = _ALIASES.get(found.group(1), found.group(1))
    if mime not in _EXTENSIONS:
        raise ImageError(
            _NOT_SEEN + "unsupported image type; supported types are PNG, JPEG, "
            "GIF and WebP."
        )
    encoded = found.group(2)
    if len(encoded) % 4:
        raise ImageError(_NOT_SEEN + "invalid base64 image data.")
    if len(encoded) // 4 * 3 > MAX_IMAGE_BYTES + 2:
        raise ImageError(
            _NOT_SEEN + f"image exceeds the {MAX_IMAGE_BYTES // 1024 // 1024} MiB "
            "per-image limit."
        )
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ImageError(_NOT_SEEN + "invalid base64 image data.") from None
    if not data:
        raise ImageError(_NOT_SEEN + "image is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(
            _NOT_SEEN + f"image exceeds the {MAX_IMAGE_BYTES // 1024 // 1024} MiB "
            "per-image limit."
        )
    if not _signature_matches(mime, data):
        raise ImageError(_NOT_SEEN + "image bytes do not match the declared type.")
    return DecodedImage(mime, data)


def placeholder(image: DecodedImage) -> dict:
    """Stable text standing in for an image in the native transport."""
    return {
        "type": "text",
        "text": (
            f"[Image attached by Hermes: {image.mime}, {len(image.data):,} bytes. "
            "It has not been shown to you yet. Call mcp__hermesbridge__read_image"
            f'(handle="{image.handle}") to view it before answering about it.]'
        ),
    }


def _private_dir(root: Path) -> Path:
    target = Path(root) / IMAGES_DIR
    target.mkdir(mode=0o700, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise ImageError(_NOT_SEEN + "image store is not a private directory.")
    if os.name != "nt":
        os.chmod(target, 0o700)
    return target


def _intact(target: Path, data: bytes) -> bool:
    """True only for an unmodified, private, single-link copy of ``data``."""
    try:
        before = os.lstat(target)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return False
        if before.st_size != len(data):
            return False
        if os.name != "nt" and (before.st_mode & 0o077 or before.st_uid != os.getuid()):
            return False
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(target, flags)
        with os.fdopen(fd, "rb") as file:
            return file.read(len(data) + 1) == data
    except OSError:
        return False


def _write_private(target: Path, data: bytes) -> None:
    # Content-addressed names are only trustworthy if the bytes are verified:
    # same size is not same content, so a tampered blob is replaced.
    if _intact(target, data):
        return
    temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def purge_images(runtime) -> None:
    """Delete every stored image blob under a runtime directory."""
    target = Path(runtime) / IMAGES_DIR
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


def externalize_images(messages: list[dict], runtime) -> list[dict]:
    """Return native-transport messages with images replaced by handles.

    Canonical ``messages`` are not modified. The store is synchronized to the
    exact images present here, so handles for images Hermes no longer sends are
    deleted. Raises ``ImageError`` (nothing is silently dropped).
    """
    images: dict[str, DecodedImage] = {}
    total = 0
    result = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or not any(map(is_image_part, content)):
            result.append(message)
            continue
        if message.get("role") not in ("user", "tool"):
            raise ImageError(
                _NOT_SEEN + "images are only supported in user and tool messages."
            )
        parts = []
        for part in content:
            if not is_image_part(part):
                parts.append(part)
                continue
            image = decode_image_part(part)
            if image.handle not in images:
                total += len(image.data)
                images[image.handle] = image
                if len(images) > MAX_IMAGES:
                    raise ImageError(
                        _NOT_SEEN + f"a request may carry at most {MAX_IMAGES} images."
                    )
                if total > MAX_TOTAL_IMAGE_BYTES:
                    raise ImageError(
                        _NOT_SEEN + "the request's images exceed the "
                        f"{MAX_TOTAL_IMAGE_BYTES // 1024 // 1024} MiB total limit."
                    )
            parts.append(placeholder(image))
        result.append({**message, "content": parts})
    if runtime is None:
        if images:
            raise ImageError(_NOT_SEEN + "native runtime unavailable for images.")
        return result
    root = Path(runtime)
    if not images:
        if (root / IMAGES_DIR).exists() or (root / IMAGES_DIR).is_symlink():
            try:
                purge_images(root)
            except OSError:
                raise ImageError(
                    _NOT_SEEN + "stale image handles could not be cleared."
                ) from None
        return result
    try:
        directory = _private_dir(root)
        wanted = {image.filename for image in images.values()}
        for entry in directory.iterdir():
            if entry.name not in wanted:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        for image in images.values():
            _write_private(directory / image.filename, image.data)
    except ImageError:
        raise
    except OSError:
        raise ImageError(_NOT_SEEN + "image could not be stored for reading.") from None
    return result

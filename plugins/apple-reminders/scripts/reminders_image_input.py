#!/usr/bin/env python3
"""Bound and decode-check local image inputs before ReminderKit sees them."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from bounded_process import (  # noqa: E402
    ProcessError,
    ProcessLaunchError,
    ProcessTimeoutError,
    run as run_bounded_process,
)


MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_PATH_LENGTH = 4_096
IMAGE_INSPECTION_TIMEOUT_SECONDS = 10
IMAGE_INSPECTION_OUTPUT_LIMIT_BYTES = 64 * 1024
SUPPORTED_IMAGE_FORMATS = frozenset({"jpeg", "png"})


class ImageInputError(ValueError):
    """The local image did not satisfy the public attachment boundary."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ValidatedImage:
    path: Path
    format: Literal["jpeg", "png"]
    bytes: int
    width: int
    height: int
    sha256: str


def _fail(reason_code: str, message: str) -> ImageInputError:
    return ImageInputError(reason_code, message)


def _inspect_image(path: Path) -> tuple[str, int, int]:
    try:
        completed = run_bounded_process(
            [
                "sips",
                "-g",
                "format",
                "-g",
                "pixelWidth",
                "-g",
                "pixelHeight",
                str(path),
            ],
            timeout_s=IMAGE_INSPECTION_TIMEOUT_SECONDS,
            stdout_limit=IMAGE_INSPECTION_OUTPUT_LIMIT_BYTES,
            stderr_limit=IMAGE_INSPECTION_OUTPUT_LIMIT_BYTES,
            output="utf8",
        )
    except ProcessTimeoutError as exc:
        raise _fail(
            "image_inspection_timeout",
            f"Image metadata inspection timed out for {path.name}",
        ) from exc
    except ProcessLaunchError as exc:
        raise _fail(
            "image_decode_failed",
            f"Image metadata inspection could not start for {path.name}",
        ) from exc
    except ProcessError as exc:
        raise _fail(
            "image_decode_failed",
            f"Image metadata inspection returned invalid output for {path.name}",
        ) from exc
    if completed.returncode != 0:
        raise _fail(
            "image_decode_failed",
            f"The image could not be decoded: {path.name}",
        )

    values: dict[str, str] = {}
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in {"format", "pixelWidth", "pixelHeight"}:
            values[key] = value.strip()
    try:
        image_format = values["format"].casefold()
        width = int(values["pixelWidth"])
        height = int(values["pixelHeight"])
    except (KeyError, ValueError) as exc:
        raise _fail(
            "image_decode_failed",
            f"The image metadata was incomplete: {path.name}",
        ) from exc
    if image_format == "jpg":
        image_format = "jpeg"
    return image_format, width, height


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def validate_image_input(value: str | os.PathLike[str]) -> ValidatedImage:
    """Return stable metadata for one absolute, regular PNG or JPEG file."""

    raw = os.fspath(value)
    if len(raw) > MAX_IMAGE_PATH_LENGTH:
        raise _fail("image_path_too_long", "Image path exceeds 4096 characters")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise _fail("absolute_path_required", "Image path must be absolute")
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise _fail("image_not_found", f"Image not found: {path.name}") from exc
    except OSError as exc:
        raise _fail("image_unreadable", f"Image metadata is unreadable: {path.name}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise _fail("symlink_not_allowed", "Image symlinks are not allowed")
    if not stat.S_ISREG(before.st_mode):
        raise _fail("regular_file_required", "Image path must name a regular file")
    if before.st_size <= 0:
        raise _fail("empty_image", "Image file must not be empty")
    if before.st_size > MAX_IMAGE_BYTES:
        raise _fail(
            "image_too_large",
            f"Image exceeds the {MAX_IMAGE_BYTES}-byte limit",
        )

    image_format, width, height = _inspect_image(path)
    if image_format not in SUPPORTED_IMAGE_FORMATS:
        raise _fail(
            "unsupported_image_format",
            "Only decoded PNG and JPEG images are supported",
        )
    if (
        width < 1
        or height < 1
        or width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
    ):
        raise _fail(
            "image_dimensions_exceeded",
            f"Image dimensions must be between 1 and {MAX_IMAGE_DIMENSION} pixels",
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise _fail(
            "image_pixel_count_exceeded",
            f"Image exceeds the {MAX_IMAGE_PIXELS}-pixel limit",
        )

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.lstat()
    except OSError as exc:
        raise _fail("image_unreadable", f"Image is unreadable: {path.name}") from exc
    if stat.S_ISLNK(after.st_mode) or _identity(before) != _identity(after):
        raise _fail(
            "image_changed_during_validation",
            "Image changed while it was being validated; retry with a stable file",
        )

    resolved = path.resolve(strict=True)
    resolved_metadata = resolved.stat()
    if _identity(after) != _identity(resolved_metadata):
        raise _fail(
            "image_changed_during_validation",
            "Image identity changed while it was being validated",
        )
    return ValidatedImage(
        path=resolved,
        format=image_format,
        bytes=after.st_size,
        width=width,
        height=height,
        sha256=digest.hexdigest(),
    )

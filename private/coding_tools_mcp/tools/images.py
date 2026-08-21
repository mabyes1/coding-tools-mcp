from __future__ import annotations

import base64
import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import ToolFailure
from ..tool_schemas import IMAGE_RESIZE_MAX_DIMENSION
from ..workspace import ResolvedPath


ResolveExisting = Callable[[str], ResolvedPath]


def view_image_tool(args: dict[str, Any], *, resolve_existing: ResolveExisting) -> dict[str, Any]:
    resolved = resolve_existing(str(args.get("path", "")))
    max_bytes = int(args.get("max_bytes", 5_242_880))
    max_width = int(args.get("max_width", IMAGE_RESIZE_MAX_DIMENSION))
    max_height = int(args.get("max_height", IMAGE_RESIZE_MAX_DIMENSION))
    auto_resize = bool(args.get("auto_resize", True))
    data = resolved.path.read_bytes()
    mime_type, width, height = identify_image(data, resolved.path)
    if mime_type is None:
        raise ToolFailure("BINARY_FILE", "File is not a supported image.", category="validation")
    original = {"bytes": len(data), "width": width, "height": height, "mime_type": mime_type}
    resized = False
    warnings: list[str] = []
    if auto_resize and should_resize_image(len(data), width, height, max_bytes, max_width, max_height):
        resized_data = resize_image_bytes(data, mime_type, max_width=max_width, max_height=max_height, max_bytes=max_bytes)
        if resized_data is not None:
            data, mime_type = resized_data
            mime_type, width, height = identify_image(data, resolved.path)
            resized = True
        else:
            warnings.append("auto_resize requested but Pillow is not installed or image resize failed")
    if len(data) > max_bytes:
        raise ToolFailure(
            "OUTPUT_TOO_LARGE",
            "Image exceeds max_bytes.",
            category="validation",
            details={"bytes": len(data), "max_bytes": max_bytes, "resize_attempted": auto_resize, "warnings": warnings},
        )
    payload: dict[str, Any] = {
        "path": resolved.display,
        "mime_type": mime_type,
        "bytes": len(data),
        "width": width,
        "height": height,
        "resized": resized,
        "original": original,
        "_mcp_image_data": base64.b64encode(data).decode("ascii"),
        "warnings": warnings,
    }
    return payload


def identify_image(data: bytes, path: Path) -> tuple[str | None, int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return "image/png", width, height
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return "image/gif", width, height
    if data.startswith(b"\xff\xd8"):
        image_width, image_height = identify_jpeg_size(data)
        return "image/jpeg", image_width, image_height
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        image_width, image_height = identify_webp_size(data)
        return "image/webp", image_width, image_height
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed, None, None
    return None, None, None


def identify_jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    while index + 9 < len(data):
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA or index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        } and segment_length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height
        index += segment_length
    return None, None


def identify_webp_size(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 30:
        return None, None
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None, None


def should_resize_image(
    size_bytes: int,
    width: int | None,
    height: int | None,
    max_bytes: int,
    max_width: int,
    max_height: int,
) -> bool:
    if size_bytes > max_bytes:
        return True
    if width is not None and width > max_width:
        return True
    if height is not None and height > max_height:
        return True
    return False


def resize_image_bytes(
    data: bytes,
    mime_type: str,
    *,
    max_width: int,
    max_height: int,
    max_bytes: int,
) -> tuple[bytes, str] | None:
    try:
        from io import BytesIO
        from PIL import Image  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        image = Image.open(BytesIO(data))
        image.thumbnail((max_width, max_height))
        output = BytesIO()
        output_format = "JPEG" if mime_type == "image/jpeg" else "PNG" if mime_type == "image/png" else "WEBP"
        save_kwargs: dict[str, Any] = {}
        if output_format in {"JPEG", "WEBP"}:
            save_kwargs["quality"] = 85
            save_kwargs["optimize"] = True
        if output_format == "JPEG" and image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        image.save(output, format=output_format, **save_kwargs)
        resized = output.getvalue()
        if len(resized) > max_bytes and output_format in {"JPEG", "WEBP"}:
            for quality in (75, 65, 55):
                output = BytesIO()
                image.save(output, format=output_format, quality=quality, optimize=True)
                resized = output.getvalue()
                if len(resized) <= max_bytes:
                    break
        return resized, mime_type
    except Exception:
        return None


__all__ = [
    "identify_image",
    "identify_jpeg_size",
    "identify_webp_size",
    "resize_image_bytes",
    "should_resize_image",
    "view_image_tool",
]

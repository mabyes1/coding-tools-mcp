from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


def run_image_checks(server: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="coding-tools-image-contract-") as temporary:
        image_workspace = Path(temporary)
        image_path = image_workspace / "pixel.png"
        image_path.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + b"\x00" * 8
            + (1).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
        )
        binary_path = image_workspace / "not-image.bin"
        binary_path.write_bytes(b"definitely-not-an-image")
        image_runtime = server.Runtime(image_workspace)
        try:
            image_payload = image_runtime.view_image({"path": "pixel.png"})
            if image_payload.get("path") != "pixel.png":
                raise RuntimeError("view_image path display contract drifted")
            if image_payload.get("mime_type") != "image/png":
                raise RuntimeError("view_image PNG mime detection drifted")
            if image_payload.get("width") != 1 or image_payload.get("height") != 1:
                raise RuntimeError("view_image PNG dimensions drifted")
            if image_payload.get("resized") is not False or image_payload.get("warnings") != []:
                raise RuntimeError("view_image no-resize result contract drifted")
            if not image_payload.get("_mcp_image_data"):
                raise RuntimeError("view_image stopped producing MCP image content data")
            try:
                image_runtime.view_image({"path": "not-image.bin"})
            except server.ToolFailure as exc:
                if exc.code != "BINARY_FILE":
                    raise
            else:
                raise RuntimeError("view_image accepted an unsupported binary file")
            try:
                image_runtime.view_image({"path": "pixel.png", "max_bytes": 8, "auto_resize": False})
            except server.ToolFailure as exc:
                if exc.code != "OUTPUT_TOO_LARGE":
                    raise
            else:
                raise RuntimeError("view_image max_bytes guard drifted")
        finally:
            image_runtime.close()

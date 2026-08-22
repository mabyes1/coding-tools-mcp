from __future__ import annotations

from typing import Any


class ToolFailure(Exception):
    """A recoverable tool-domain failure that should be shown to the agent."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "runtime",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.retryable = retryable
        self.details = details or {}


class JsonRpcError(Exception):
    """A JSON-RPC protocol failure with an optional structured data payload."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def summarize_exception(exc: BaseException) -> tuple[str, list[str]]:
    """Expose useful leaf errors instead of opaque ExceptionGroup/TaskGroup text."""

    leaves: list[str] = []

    def collect(current: BaseException) -> None:
        if isinstance(current, BaseExceptionGroup):
            for child in current.exceptions:
                collect(child)
            return
        message = str(current).strip() or current.__class__.__name__
        leaves.append(f"{current.__class__.__name__}: {message}")

    collect(exc)
    unique: list[str] = []
    for leaf in leaves:
        if leaf not in unique:
            unique.append(leaf)
    if not unique:
        unique = [f"{exc.__class__.__name__}: {str(exc).strip() or 'unknown error'}"]
    summary = unique[0] if len(unique) == 1 else " | ".join(unique[:4])
    return summary, unique[:16]


__all__ = ["JsonRpcError", "ToolFailure", "summarize_exception"]

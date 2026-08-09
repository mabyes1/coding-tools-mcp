from __future__ import annotations

from pathlib import Path


def git_command(git: str, safe_directory: Path, *args: str) -> list[str]:
    """Build a git command that trusts only one selected repository path.

    The Windows service runs as LOCAL SERVICE while repositories are owned by
    the interactive user. Git therefore rejects otherwise-safe repository
    reads with its dubious-ownership guard unless safe.directory is supplied.
    Passing the setting with ``-c`` keeps the exception process-local instead
    of mutating machine or user Git configuration.
    """

    root = safe_directory.expanduser().resolve(strict=True)
    return [git, "-c", f"safe.directory={root}", *args]

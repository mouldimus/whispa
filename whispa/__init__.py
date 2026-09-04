"""whispa - offline dictation with a global hotkey."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

# The release line. It is hand-bumped only for milestones, so on its own it
# cannot answer "did the update I just ran actually change anything?" -
# build_id() adds the commit for that.
__version__ = "5.1"


def describe_commit(root: Path | None = None, timeout: float = 5) -> str | None:
    """'6270b4b 2026-09-04' for the checkout at `root`, or None if there is
    no git, no checkout, or anything else goes wrong."""
    root = root or Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h %cs"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stdout.strip()
    return text if result.returncode == 0 and text else None


def format_build(version: str, commit: str | None) -> str:
    """'5.1 @ 6270b4b 2026-09-04', or just '5.1' for a folder-copy install."""
    return f"{version} @ {commit}" if commit else version


@lru_cache(maxsize=1)
def _commit() -> str | None:
    return describe_commit()


def build_id() -> str:
    """What is actually running, for the tray and the log.

    Deployment is a git pull, so the commit is the version that matters;
    the tray saying "5.1" before and after an update told nobody anything.
    """
    return format_build(__version__, _commit())


def build_short() -> str:
    """The commit hash alone ('6270b4b'), for the pill, which shows about
    twenty characters; the version number if there is no checkout."""
    commit = _commit()
    return commit.split()[0] if commit else __version__

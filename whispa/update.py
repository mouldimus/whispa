"""Auto-update: pull the latest commit from git, resync deps, relaunch.

whispa is deployed by cloning a private git repo onto each machine rather
than copying the folder by hand, so "ship a fix" means "push a commit" and
every machine picks it up the next time it starts - no separate installer
step, no going round to each PC.

Everything here is best-effort and silent. A machine that is offline, has no
git on its PATH, was never set up as a clone (still just a folder copy), or
has local edits in the way must start exactly as if this module did not
exist - dictation must never fail to start because GitHub is unreachable.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, NamedTuple, Protocol

log = logging.getLogger(__name__)

_FETCH_TIMEOUT = 8
_LOCAL_TIMEOUT = 5
_PULL_TIMEOUT = 20
_PIP_TIMEOUT = 300


class CommandResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(self, args: list[str], cwd: Path, timeout: float) -> CommandResult: ...


def run_subprocess(args: list[str], cwd: Path, timeout: float) -> CommandResult:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        return CommandResult(-1, "", "timed out")
    except OSError as exc:
        return CommandResult(-1, "", str(exc))


def project_root() -> Path:
    """The folder holding the `whispa` package - i.e. the git checkout root."""
    return Path(__file__).resolve().parent.parent


def parse_behind_count(text: str) -> int | None:
    """Parse the output of `git rev-list HEAD..origin/<branch> --count`."""
    text = text.strip()
    return int(text) if text.isdigit() else None


def is_clean(status_output: str) -> bool:
    """True when `git status --porcelain` reports no local changes.

    A machine with edits in the working tree - someone poking at config
    defaults, say - must not have them silently overwritten or a pull
    silently refused halfway through; skip the update entirely instead.
    """
    return status_output.strip() == ""


class AutoUpdater:
    def __init__(
        self,
        root: Path | None = None,
        run: CommandRunner | None = None,
    ) -> None:
        self.root = root or project_root()
        self.run = run or run_subprocess

    def is_git_checkout(self) -> bool:
        return (self.root / ".git").exists()

    def _current_branch(self) -> str | None:
        result = self.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], self.root, _LOCAL_TIMEOUT
        )
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        return branch if branch and branch != "HEAD" else None

    def _requirements_text(self) -> str | None:
        req = self.root / "requirements.txt"
        try:
            return req.read_text(encoding="utf-8")
        except OSError:
            return None

    def check_and_apply(self) -> bool:
        """Fetch, fast-forward if behind, resync dependencies if they
        changed. Returns True when the checkout was updated, meaning the
        caller should relaunch to run the new code."""
        if not self.is_git_checkout():
            return False

        try:
            branch = self._current_branch()
            if branch is None:
                return False

            status = self.run(["git", "status", "--porcelain"], self.root, _LOCAL_TIMEOUT)
            if status.returncode != 0:
                return False
            if not is_clean(status.stdout):
                log.info("local changes present in %s; skipping auto-update", self.root)
                return False

            fetch = self.run(
                ["git", "fetch", "--quiet", "origin", branch], self.root, _FETCH_TIMEOUT
            )
            if fetch.returncode != 0:
                log.debug("update check could not reach origin: %s", fetch.stderr.strip())
                return False

            count = self.run(
                ["git", "rev-list", f"HEAD..origin/{branch}", "--count"],
                self.root,
                _LOCAL_TIMEOUT,
            )
            behind = parse_behind_count(count.stdout) if count.returncode == 0 else None
            if not behind:
                return False

            req_before = self._requirements_text()
            pull = self.run(
                ["git", "pull", "--ff-only", "--quiet", "origin", branch],
                self.root,
                _PULL_TIMEOUT,
            )
            if pull.returncode != 0:
                log.warning("update pull failed, staying on the current version: %s", pull.stderr.strip())
                return False

            log.info("updated %d commit(s) from origin/%s", behind, branch)
            if self._requirements_text() != req_before:
                self._sync_dependencies()
            return True
        except Exception:
            log.warning("auto-update check failed", exc_info=True)
            return False

    def _sync_dependencies(self) -> None:
        python = self.root / ".venv" / "Scripts" / "python.exe"
        if not python.exists():
            python = Path(sys.executable)
        result = self.run(
            [str(python), "-m", "pip", "install", "-q", "-r", str(self.root / "requirements.txt")],
            self.root,
            _PIP_TIMEOUT,
        )
        if result.returncode != 0:
            log.warning("dependency sync failed: %s", result.stderr.strip())
        else:
            log.info("dependencies resynced")


def check_and_apply(root: Path | None = None) -> bool:
    return AutoUpdater(root).check_and_apply()


def relaunch() -> None:
    """Re-exec this process so the just-pulled code runs immediately,
    instead of waiting for the next manual start."""
    log.info("relaunching to run the updated version")
    logging.shutdown()
    os.execv(sys.executable, [sys.executable] + sys.argv)

"""Auto-update: pull the latest commit from git, resync deps, relaunch.

whispa is deployed by cloning a git repo onto each machine rather
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

# A cold fetch on Windows can spend a while in the credential helper and
# TLS handshake before any bytes move; 8s proved too tight.
_FETCH_TIMEOUT = 20
_LOCAL_TIMEOUT = 5
_PULL_TIMEOUT = 20
_PIP_TIMEOUT = 300


class CommandResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class Outcome(NamedTuple):
    """What an update check did, and a one-line reason fit for the tray.

    Every way the check can stop short has a distinct message: "already up
    to date" for a real no-op, and something actionable otherwise - "run
    install.bat", "offline?", "local edits" - because a button that answers
    "up to date" when it actually skipped is indistinguishable from a broken
    one.
    """

    updated: bool
    message: str


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

    The status is taken with --untracked-files=no: a stray file someone
    dropped in the folder (a log, a note, a leftover from a folder-copy
    install) is not an edit and does not stop a fast-forward pull.
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
        return self.check().updated

    def check(self) -> Outcome:
        """check_and_apply, with the reason it stopped where it did."""
        if not self.is_git_checkout():
            # The single most useful line in the log when a machine is stuck
            # on an old version: it was deployed as a folder copy and has no
            # way to update until install.bat converts it.
            log.info(
                "%s is not a git checkout - auto-update is off. "
                "Run install.bat once to enable it.",
                self.root,
            )
            return Outcome(False, "not a git checkout - run install.bat once to enable updates")

        try:
            branch = self._current_branch()
            if branch is None:
                log.info("git is missing or this checkout is broken; skipping auto-update")
                return Outcome(False, "git not found, or the checkout is broken - see log")

            status = self.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                self.root,
                _LOCAL_TIMEOUT,
            )
            if status.returncode != 0:
                log.info("git status failed; skipping auto-update: %s", status.stderr.strip())
                return Outcome(False, "git status failed - see log")
            if not is_clean(status.stdout):
                log.info(
                    "local changes present in %s; skipping auto-update:\n%s",
                    self.root,
                    status.stdout.strip(),
                )
                return Outcome(False, "local edits in the whispa folder - update skipped")

            fetch = self.run(
                ["git", "fetch", "--quiet", "origin", branch], self.root, _FETCH_TIMEOUT
            )
            if fetch.returncode != 0:
                log.info(
                    "update check could not reach origin (offline?): %s",
                    fetch.stderr.strip(),
                )
                return Outcome(False, "couldn't reach GitHub - offline?")

            count = self.run(
                ["git", "rev-list", f"HEAD..origin/{branch}", "--count"],
                self.root,
                _LOCAL_TIMEOUT,
            )
            if count.returncode != 0:
                log.info("could not compare with origin/%s: %s", branch, count.stderr.strip())
                return Outcome(False, "couldn't compare with GitHub - see log")
            behind = parse_behind_count(count.stdout)
            if not behind:
                return Outcome(False, "already up to date")

            req_before = self._requirements_text()
            pull = self.run(
                ["git", "pull", "--ff-only", "--quiet", "origin", branch],
                self.root,
                _PULL_TIMEOUT,
            )
            if pull.returncode != 0:
                log.warning("update pull failed, staying on the current version: %s", pull.stderr.strip())
                return Outcome(False, "update failed to apply - see log")

            log.info("updated %d commit(s) from origin/%s", behind, branch)
            if self._requirements_text() != req_before:
                self._sync_dependencies()
            return Outcome(True, f"updated ({behind} commit{'s' if behind != 1 else ''})")
        except Exception:
            log.warning("auto-update check failed", exc_info=True)
            return Outcome(False, "update check crashed - see log")

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
    instead of waiting for the next manual start.

    Only safe at startup, before the overlay, tray and hotkey exist - a
    running instance restarts via spawn_replacement() plus a normal quit
    instead.
    """
    log.info("relaunching to run the updated version")
    logging.shutdown()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def spawn_replacement(popen=None) -> bool:
    """Start a fresh whispa process, for restarting after a mid-run update.

    The caller quits this instance once the new one is launched. The two
    overlap for a moment, which is harmless - the newcomer spends its first
    seconds loading the model while this one tears down. Prefers the .vbs
    launcher for the same reason autostart does: it sets its own working
    directory and opens no window.
    """
    popen = popen or subprocess.Popen
    root = project_root()
    vbs = root / "whispa-silent.vbs"
    try:
        if sys.platform == "win32" and vbs.exists():
            popen(["wscript.exe", str(vbs)], cwd=str(root), close_fds=True)
        else:
            popen(
                [sys.executable, "-m", "whispa"] + sys.argv[1:],
                cwd=str(root),
                close_fds=True,
            )
        log.info("replacement process started")
        return True
    except Exception:
        log.exception("could not start the replacement process")
        return False

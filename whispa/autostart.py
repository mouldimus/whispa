"""Starting whispa when Windows starts.

Uses the per-user Run key rather than a shortcut in the Startup folder:
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run. It needs no admin
rights, it is a single value to add or remove so the toggle is exactly
reversible, and creating a .lnk would otherwise drag in a COM dependency just
to write one file.

The registry is reached through a tiny backend object so the command building
and toggle logic can be tested on a machine that has no registry at all.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "whispa"


class RegistryBackend(Protocol):
    def read(self, name: str) -> str | None: ...
    def write(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...
    @property
    def available(self) -> bool: ...


class WindowsRegistry:
    """The real thing. Every call is guarded: a failure here must never be
    worse than the toggle not working."""

    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    def read(self, name: str) -> str | None:
        if not self.available:
            return None
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _type = winreg.QueryValueEx(key, name)
                return str(value)
        except FileNotFoundError:
            return None
        except Exception:
            log.debug("could not read the Run key", exc_info=True)
            return None

    def write(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


class MemoryRegistry:
    """Stand-in used by the tests and on non-Windows platforms."""

    available = True

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(initial or {})

    def read(self, name: str) -> str | None:
        return self.values.get(name)

    def write(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def project_root() -> Path:
    """The folder holding the `whispa` package - i.e. where the .bat files are."""
    return Path(__file__).resolve().parent.parent


def find_pythonw(root: Path | None = None) -> Path | None:
    """Locate the windowed interpreter inside the project's venv."""
    root = root or project_root()
    candidate = root / ".venv" / "Scripts" / "pythonw.exe"
    if candidate.exists():
        return candidate
    # Running outside the bundled venv: derive it from the interpreter in use.
    current = Path(sys.executable)
    sibling = current.with_name("pythonw.exe")
    return sibling if sibling.exists() else None


def build_launch_command(root: Path, pythonw: Path | None) -> str:
    """The command Windows should run at login.

    Prefers the .vbs launcher, which sets its own working directory and starts
    the interpreter with no window at all. `pythonw -m whispa` on its own would
    fail, because -m needs the folder containing the package on the path and a
    Run entry cannot set a working directory.
    """
    vbs = root / "whispa-silent.vbs"
    if vbs.exists():
        return f'wscript.exe "{vbs}"'
    if pythonw is not None:
        # Fallback: inject the path explicitly rather than relying on the cwd.
        return (
            f'"{pythonw}" -c "import sys; sys.path.insert(0, r\'{root}\'); '
            f'import whispa.__main__ as m; sys.exit(m.main())"'
        )
    raise RuntimeError("no way to launch whispa was found")


class AutostartManager:
    def __init__(
        self,
        backend: RegistryBackend | None = None,
        root: Path | None = None,
        command: str | None = None,
    ) -> None:
        self.backend = backend or WindowsRegistry()
        self.root = root or project_root()
        self._command = command

    @property
    def available(self) -> bool:
        return bool(getattr(self.backend, "available", False))

    @property
    def command(self) -> str:
        if self._command is None:
            self._command = build_launch_command(self.root, find_pythonw(self.root))
        return self._command

    def is_enabled(self) -> bool:
        return self.backend.read(VALUE_NAME) is not None

    def is_stale(self) -> bool:
        """True when autostart points at a different copy of whispa.

        Happens after the folder is moved, and would otherwise silently launch
        the old location - or nothing - every morning.
        """
        current = self.backend.read(VALUE_NAME)
        if current is None:
            return False
        try:
            return current.strip() != self.command.strip()
        except RuntimeError:
            return False

    def enable(self) -> bool:
        try:
            self.backend.write(VALUE_NAME, self.command)
            log.info("autostart enabled: %s", self.command)
            return True
        except Exception:
            log.exception("could not enable autostart")
            return False

    def disable(self) -> bool:
        try:
            self.backend.delete(VALUE_NAME)
            log.info("autostart disabled")
            return True
        except Exception:
            log.exception("could not disable autostart")
            return False

    def toggle(self) -> bool:
        """Flip the setting. Returns the state it ended up in."""
        if self.is_enabled() and not self.is_stale():
            self.disable()
            return False
        self.enable()
        return True

    def repair_if_stale(self) -> bool:
        """Re-point a moved installation. Returns True if something changed."""
        if self.is_enabled() and self.is_stale():
            log.info("autostart pointed at an old location; updating it")
            return self.enable()
        return False

"""System-tray icon.

Purely a status surface: a coloured dot that tells you whether the app is
listening, thinking, or broken, plus a menu to quit. Everything still works
with --no-tray if pystray misbehaves, which it sometimes does on Windows.
"""

from __future__ import annotations

import logging
from typing import Callable

from . import __version__
from .app import State
from .hotkey import PRESETS, parse_hotkey, preset_label

log = logging.getLogger(__name__)

_COLOURS = {
    State.IDLE: (110, 110, 115),
    State.RECORDING: (220, 60, 60),
    State.TRANSCRIBING: (230, 170, 40),
    State.ERROR: (200, 40, 160),
}


def _make_image(state: State, size: int = 64):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = size // 8
    draw.ellipse((pad, pad, size - pad, size - pad), fill=_COLOURS.get(state, (110, 110, 115)))
    if state is State.RECORDING:
        # A hollow ring reads as "live" at 16px far better than a solid dot.
        inner = size // 3
        draw.ellipse((inner, inner, size - inner, size - inner), fill=(255, 255, 255, 230))
    return image


class TrayIcon:
    def __init__(
        self,
        hotkey: str,
        model: str,
        mode: str,
        on_quit: Callable[[], None],
        on_fix_last: Callable[[], None] | None = None,
        learned_stats: Callable[[], dict] | None = None,
        on_open_console: Callable[[], None] | None = None,
        autostart=None,
        on_set_hotkey: Callable[[str], None] | None = None,
    ) -> None:
        self.hotkey = hotkey
        self.model = model
        self.mode = mode
        self.on_quit = on_quit
        self.on_fix_last = on_fix_last
        self.learned_stats = learned_stats
        self.on_open_console = on_open_console
        self.autostart = autostart
        self.on_set_hotkey = on_set_hotkey
        self._icon = None
        self._status = "ready"

    def _usage_hint(self, _item=None) -> str:
        label = preset_label(self.hotkey)
        if self.mode == "hybrid":
            return f"Tap or hold [{label}] to dictate"
        if self.mode == "toggle":
            return f"Press [{label}] to start and stop"
        return f"Hold [{label}] to dictate"

    def _learned_label(self, _item=None) -> str:
        if self.learned_stats is None:
            return "Learning: off"
        try:
            stats = self.learned_stats()
        except Exception:
            return "Learning: unavailable"
        return f"Learned: {stats.get('active', 0)} active, {stats.get('tracked', 0)} tracked"

    def _build(self):
        import pystray

        items = [
            pystray.MenuItem(lambda _: f"Status: {self._status}", None, enabled=False),
            pystray.MenuItem(self._usage_hint, None, enabled=False),
            pystray.MenuItem(
                f"Model: {self.model}   (whispa {__version__})", None, enabled=False
            ),
            pystray.MenuItem(self._learned_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        if self.on_fix_last is not None:
            items.append(
                pystray.MenuItem("Fix last dictation...", self._fix_last, default=True)
            )
        settings = self._settings_menu(pystray)
        if settings is not None:
            items.append(pystray.MenuItem("Settings", settings))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Quit", self._quit))
        return pystray.Icon(
            "whispa", _make_image(State.IDLE), "whispa - ready", pystray.Menu(*items)
        )

    def _is_current(self, spec: str) -> bool:
        try:
            return parse_hotkey(spec) == parse_hotkey(self.hotkey)
        except ValueError:
            return False

    def _shortcut_menu(self, pystray):
        """Radio list of the offered shortcuts, plus whatever is configured.

        Changing it here rebinds the listener immediately and writes the choice
        to config.json - a shortcut you have to restart the app to try is a
        shortcut nobody tries.
        """
        entries = []
        specs = [spec for _label, spec in PRESETS]
        if not any(self._is_current(spec) for spec in specs):
            # A hand-edited config keeps its own entry, so choosing a preset
            # and changing your mind is not a one-way door.
            specs.insert(0, self.hotkey)
        for spec in specs:
            entries.append(
                pystray.MenuItem(
                    preset_label(spec),
                    self._set_hotkey(spec),
                    checked=lambda _item, spec=spec: self._is_current(spec),
                    radio=True,
                )
            )
        return pystray.Menu(*entries)

    def _set_hotkey(self, spec: str):
        def handler(_icon=None, _item=None) -> None:
            if self.on_set_hotkey is None:
                return
            try:
                self.on_set_hotkey(spec)
                self.hotkey = spec
                self._status = f"shortcut: {preset_label(spec)}"
            except Exception:
                log.exception("could not change the shortcut to %r", spec)
                self._status = "shortcut change failed - see log"
            if self._icon is not None:
                self._icon.update_menu()

        return handler

    def _settings_menu(self, pystray):
        entries = []
        if self.on_set_hotkey is not None:
            entries.append(
                pystray.MenuItem("Shortcut", self._shortcut_menu(pystray))
            )
        if self.on_open_console is not None:
            entries.append(
                pystray.MenuItem("Open debug console...", self._open_console)
            )
        if self.autostart is not None and self.autostart.available:
            entries.append(
                pystray.MenuItem(
                    "Start with Windows",
                    self._toggle_autostart,
                    checked=lambda _item: self._autostart_checked(),
                )
            )
        return pystray.Menu(*entries) if entries else None

    def _autostart_checked(self) -> bool:
        try:
            return self.autostart.is_enabled()
        except Exception:
            log.debug("could not read the autostart setting", exc_info=True)
            return False

    def _toggle_autostart(self, _icon=None, _item=None) -> None:
        try:
            now_on = self.autostart.toggle()
            self._status = "starts with Windows" if now_on else "manual start"
            if self._icon is not None:
                self._icon.update_menu()
        except Exception:
            log.exception("could not change the autostart setting")

    def _open_console(self, _icon=None, _item=None) -> None:
        if self.on_open_console is not None:
            try:
                self.on_open_console()
            except Exception:
                log.exception("could not open the debug console")

    def _fix_last(self, _icon=None, _item=None) -> None:
        if self.on_fix_last is not None:
            try:
                self.on_fix_last()
            except Exception:
                log.exception("could not open the correction dialog")

    def _quit(self, icon, _item) -> None:
        try:
            self.on_quit()
        finally:
            icon.stop()

    def set_state(self, state: State, detail: str = "") -> None:
        self._status = detail or state.value
        if self._icon is None:
            return
        try:
            self._icon.icon = _make_image(state)
            self._icon.title = f"whispa - {state.value}" + (f": {detail}" if detail else "")
            self._icon.update_menu()
        except Exception:
            # A failing tray must never take the dictation loop down with it.
            log.debug("tray update failed", exc_info=True)

    def run(self) -> None:
        """Blocks on the platform event loop until Quit is chosen."""
        self._icon = self._build()
        self._icon.run()

    def run_detached(self) -> None:
        """Start the icon without owning the calling thread.

        Needed because the overlay's tkinter loop wants the main thread, and
        only one of the two can have it.
        """
        self._icon = self._build()
        self._icon.run_detached()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                log.debug("tray stop failed", exc_info=True)
            self._icon = None

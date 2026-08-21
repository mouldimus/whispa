"""User-editable configuration, persisted as JSON next to the app."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# Where the JSON config lives on Windows: %APPDATA%\whispa\config.json
# (falls back to ~/.config/whispa/config.json elsewhere, e.g. when testing on Linux).
def default_config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "whispa"
    return Path.home() / ".config" / "whispa"


@dataclass
class Config:
    # --- hotkey -------------------------------------------------------------
    # A single non-modifier key is the default on purpose: push-to-talk on a
    # chord like ctrl+alt+X leaves the modifiers physically held at the moment
    # we want to send the paste, which fights the injection. F9 has no such
    # problem. Accepts pynput key names ("f9", "scroll_lock") or a chord
    # ("<ctrl>+<alt>+d") if the user insists.
    hotkey: str = "f9"
    # "hold" = push-to-talk (record while held).
    # "toggle" = press once to start, again to stop.
    hotkey_mode: str = "hold"

    # --- model --------------------------------------------------------------
    # base.en on int8 is the sweet spot for a CPU-only box: ~1x realtime and
    # good enough for dictation. small.en is noticeably better but ~2.5x slower.
    model: str = "base.en"
    device: str = "cpu"
    compute_type: str = "int8"
    # 0 = let ctranslate2 decide (it picks the physical core count).
    cpu_threads: int = 0
    language: str = "en"
    beam_size: int = 1
    # Silero VAD trims silence before decoding: on a CPU box this is most of
    # the speed win, because held-hotkey recordings are mostly leading and
    # trailing silence.
    vad_filter: bool = True
    initial_prompt: str = ""

    # --- audio --------------------------------------------------------------
    sample_rate: int = 16000
    input_device: int | None = None
    # Recordings shorter than this are treated as a mis-press and dropped.
    min_recording_seconds: float = 0.35
    max_recording_seconds: float = 300.0

    # --- injection ----------------------------------------------------------
    # "paste"  = copy to clipboard, send ctrl+v, restore the old clipboard.
    #            Fast and reliable in nearly every app.
    # "type"   = synthesise keystrokes character by character. Slower, but
    #            works in the rare app that blocks programmatic paste.
    # "clipboard" = copy only, no injection; the user pastes themselves.
    inject_method: str = "paste"
    type_delay: float = 0.005
    # Append a trailing space so consecutive dictations don't run together.
    trailing_space: bool = True
    restore_clipboard: bool = True
    # Seconds to wait for physically-held modifier keys to be released before
    # injecting. Prevents a still-held Alt turning our ctrl+v into a menu call.
    modifier_release_timeout: float = 1.0

    # --- behaviour ----------------------------------------------------------
    play_sounds: bool = True
    # Run a throwaway transcription at startup so the first real one isn't slow.
    warmup: bool = True
    log_level: str = "INFO"
    # Words/phrases rewritten after transcription, e.g. {"gonna": "going to"}.
    replacements: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or (default_config_dir() / "config.json")
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as fh:
            raw: dict[str, Any] = json.load(fh)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        # Unknown keys are ignored rather than fatal: a config written by a
        # newer build should not stop an older one from starting.
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path | None = None) -> Path:
        path = path or (default_config_dir() / "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
        return path

    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means usable."""
        problems = []
        if self.hotkey_mode not in ("hold", "toggle"):
            problems.append(f"hotkey_mode must be 'hold' or 'toggle', got {self.hotkey_mode!r}")
        if self.inject_method not in ("paste", "type", "clipboard"):
            problems.append(
                f"inject_method must be 'paste', 'type' or 'clipboard', got {self.inject_method!r}"
            )
        if self.sample_rate not in (8000, 16000, 22050, 44100, 48000):
            problems.append(f"unusual sample_rate {self.sample_rate}; whisper expects 16000")
        if self.min_recording_seconds < 0:
            problems.append("min_recording_seconds must be >= 0")
        if self.max_recording_seconds <= self.min_recording_seconds:
            problems.append("max_recording_seconds must exceed min_recording_seconds")
        if self.beam_size < 1:
            problems.append("beam_size must be >= 1")
        return problems

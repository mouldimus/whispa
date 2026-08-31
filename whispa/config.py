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
    # "hybrid" = tap to latch on (tap again to stop), or hold for push-to-talk;
    #            the key decides which by how long you held it.
    # "hold"   = push-to-talk only.
    # "toggle" = press once to start, again to stop.
    hotkey_mode: str = "hybrid"
    # Press-and-release faster than this counts as a tap rather than a hold.
    tap_seconds: float = 0.35

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

    # --- formatting ---------------------------------------------------------
    # Whisper returns one unbroken line. Structure comes from three places:
    # the pauses between segments, spoken commands ("new paragraph", "bullet
    # point"), and sentence capitalisation.
    #
    # "blank"  = paragraphs separated by a blank line (a document)
    # "single" = one newline between paragraphs
    # "off"    = never insert a newline. Use this in chat boxes where Enter
    #            sends the message.
    paragraph_style: str = "blank"
    # A silence at least this long starts a new paragraph. Two seconds is a
    # deliberate stop-and-think, not the beat between two sentences: measured
    # against real speech, ordinary rhetorical pauses run 1.0-1.5s, so a lower
    # value breaks paragraphs mid-thought. 0 disables pause-based paragraphing.
    paragraph_pause_seconds: float = 2.0
    # A shorter pause than that, where whisper still tends to drop the comma
    # it would otherwise have written (measured against real audio: a
    # 1.0-1.5s gap loses it, a 0.4-0.7s one doesn't). Off by default: the same
    # measurement also found an ordinary rhetorical pause of about a second
    # in fluent speech with no grammatical comma there at all, so a length
    # this short is not a safe universal signal - it will occasionally insert
    # a comma where none belongs. Worth trying if your own dictation tends to
    # pause where a comma *should* go; watch the log if you turn it on.
    comma_pause_seconds: float = 0.0
    # Honour spoken structure commands. See whispa/format.py for the list.
    voice_commands: bool = True
    # Extra or overriding commands, e.g. {"full stop": ".", "new section":
    # "paragraph"}. Values may be "paragraph", "line", "bullet", "number", or
    # any literal text. An empty value removes a default command.
    voice_command_extras: dict[str, str] = field(default_factory=dict)
    # Capitalise the first letter of each sentence, bullet and paragraph.
    auto_capitalise: bool = True
    bullet_prefix: str = "- "

    # --- behaviour ----------------------------------------------------------
    # --- indicator ----------------------------------------------------------
    # The on-screen pill: shows idle/recording/thinking, with a live input
    # meter while recording so a dead microphone is visible immediately.
    overlay: bool = True
    # Keep the pill on screen when idle, instead of only during dictation.
    overlay_always_visible: bool = False

    # --- learning -----------------------------------------------------------
    # Read the focused control back after injecting and learn from any edits.
    learn_from_edits: bool = True
    # How long to wait before reading back. Long enough to finish a sentence
    # and fix it, short enough that the text is still on screen.
    learn_delay_seconds: float = 6.0
    # How many times a correction must repeat before it is applied
    # automatically. 1 would act on a single change of mind.
    learn_min_count: int = 2
    # Feed learnt vocabulary to whisper as decoder bias, which prevents
    # mistakes rather than patching them afterwards.
    learn_bias_prompt: bool = True

    play_sounds: bool = True
    # Run a throwaway transcription at startup so the first real one isn't slow.
    warmup: bool = True
    # Pull updates from git on startup when this copy is a clone (see
    # whispa/update.py). No-op, silently, if it isn't - e.g. a hand-copied
    # folder rather than `git clone`.
    auto_update: bool = True
    log_level: str = "INFO"
    # With no console window there is nowhere for errors to go, so they go to a
    # file next to the config.
    log_to_file: bool = True
    # Words/phrases rewritten after transcription, e.g. {"gonna": "going to"}.
    replacements: dict[str, str] = field(default_factory=dict)
    # Strip filler words ("um", "uh"), collapse immediate stutters ("the the
    # meeting"), and drop a false start ahead of an explicit spoken
    # correction ("scratch that", "strike that", "disregard that"). See
    # whispa/format.py:remove_disfluencies for what it does and does not
    # catch.
    remove_disfluencies: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        return cls.load_checked(path)[0]

    @classmethod
    def load_checked(cls, path: Path | None = None) -> tuple["Config", str | None]:
        """Load settings, returning (config, problem).

        A hand-edited config with a stray comma must not stop whispa starting.
        There is no console window to print a traceback to, so a broken file
        falls back to defaults and reports the problem for the caller to
        surface in the indicator and the log.
        """
        path = path or (default_config_dir() / "config.json")
        if not path.exists():
            return cls(), None
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw: dict[str, Any] = json.load(fh)
        except json.JSONDecodeError as exc:
            return cls(), f"{path} is not valid JSON ({exc}); using defaults"
        except OSError as exc:
            return cls(), f"could not read {path} ({exc}); using defaults"
        if not isinstance(raw, dict):
            return cls(), f"{path} must contain a JSON object; using defaults"
        try:
            return cls.from_dict(raw), None
        except (TypeError, ValueError) as exc:
            return cls(), f"{path} has unusable values ({exc}); using defaults"

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
        if self.hotkey_mode not in ("hold", "toggle", "hybrid"):
            problems.append(
                f"hotkey_mode must be 'hybrid', 'hold' or 'toggle', "
                f"got {self.hotkey_mode!r}"
            )
        if self.tap_seconds <= 0:
            problems.append("tap_seconds must be > 0")
        if self.paragraph_style not in ("blank", "single", "off"):
            problems.append(
                f"paragraph_style must be 'blank', 'single' or 'off', "
                f"got {self.paragraph_style!r}"
            )
        if self.paragraph_pause_seconds < 0:
            problems.append("paragraph_pause_seconds must be >= 0")
        if self.comma_pause_seconds < 0:
            problems.append("comma_pause_seconds must be >= 0")
        if self.learn_min_count < 1:
            problems.append("learn_min_count must be >= 1")
        if self.learn_delay_seconds <= 0:
            problems.append("learn_delay_seconds must be > 0")
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

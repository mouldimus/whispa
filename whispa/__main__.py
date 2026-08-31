"""Entry point:  pythonw -m whispa  [--options]

Launched from whispa.bat there is no console window, so nothing here may rely
on printing: progress goes to the on-screen pill, and errors go to a log file
next to the config.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .app import DictationEngine, State
from .config import Config, default_config_dir
from .format import make_formatter
from .hotkey import GlobalHotkey
from .inject import KeyboardInjector, NullInjector
from .transcribe import WhisperTranscriber

log = logging.getLogger("whispa")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="whispa",
        description="Tap or hold a key, speak, release - the text lands in "
        "whatever window has focus. Runs entirely offline.",
    )
    p.add_argument("--config", type=Path, help="path to config.json")
    p.add_argument("--hotkey", help="e.g. f9, scroll_lock, <ctrl>+<alt>+d")
    p.add_argument(
        "--mode",
        choices=["hybrid", "hold", "toggle"],
        help="hybrid (tap to latch, hold to push-to-talk), hold, or toggle",
    )
    p.add_argument("--model", help="base.en, small.en, medium.en, large-v3 ...")
    p.add_argument("--device", help="cpu or cuda")
    p.add_argument("--compute-type", help="int8, int8_float16, float16, float32")
    p.add_argument("--input-device", type=int, help="microphone index")
    p.add_argument(
        "--inject", choices=["paste", "type", "clipboard"], help="how to deliver text"
    )
    p.add_argument(
        "--paragraphs",
        choices=["blank", "single", "off"],
        help="paragraph breaks: blank line, single newline, or never",
    )
    p.add_argument(
        "--no-voice-commands",
        action="store_true",
        help='ignore spoken structure commands ("new paragraph", "bullet point")',
    )
    p.add_argument("--dry-run", action="store_true", help="transcribe, never type")
    p.add_argument("--no-tray", action="store_true", help="no tray icon")
    p.add_argument("--no-overlay", action="store_true", help="no on-screen indicator")
    p.add_argument(
        "--show-overlay-always",
        action="store_true",
        help="keep the indicator visible when idle",
    )
    p.add_argument("--no-learn", action="store_true", help="disable learning from edits")
    p.add_argument("--console", action="store_true", help="also log to stdout")
    p.add_argument("--list-devices", action="store_true", help="list microphones, exit")
    p.add_argument("--write-config", action="store_true", help="write defaults, exit")
    p.add_argument(
        "--autostart",
        choices=["on", "off", "status"],
        help="start whispa when Windows starts (also in the tray's Settings menu)",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument(
        "--version", action="version", version=f"whispa {__version__}"
    )
    return p


def setup_logging(cfg: Config, args: argparse.Namespace):
    """Configure logging and return the in-memory buffer the tray console reads.

    The buffer is installed first and always, so that the debug console can
    show what happened *before* it was opened - which is the only history that
    matters when something has already gone wrong.
    """
    level = logging.DEBUG if args.verbose else getattr(
        logging, cfg.log_level.upper(), logging.INFO
    )
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"
    )

    from .console import RingBufferHandler

    buffer = RingBufferHandler()
    buffer.setFormatter(fmt)
    root.addHandler(buffer)
    # Console handlers are pointless under pythonw and can even raise when
    # stdout is not a real stream, so they are opt-in.
    if args.console or args.list_devices or args.write_config:
        if sys.stdout is not None:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(fmt)
            root.addHandler(handler)
    if cfg.log_to_file:
        try:
            path = default_config_dir() / "whispa.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=512_000, backupCount=2, encoding="utf-8"
            )
            handler.setFormatter(fmt)
            root.addHandler(handler)
        except Exception:
            pass
    return buffer


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    for attr, field_name in (
        ("hotkey", "hotkey"),
        ("mode", "hotkey_mode"),
        ("model", "model"),
        ("device", "device"),
        ("compute_type", "compute_type"),
        ("input_device", "input_device"),
        ("inject", "inject_method"),
        ("paragraphs", "paragraph_style"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, field_name, value)
    if args.no_overlay:
        cfg.overlay = False
    if args.show_overlay_always:
        cfg.overlay_always_visible = True
    if args.no_learn:
        cfg.learn_from_edits = False
    if args.no_voice_commands:
        cfg.voice_commands = False
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.write_config:
        path = Config().save(args.config)
        print(f"wrote defaults to {path}")
        return 0

    cfg, load_problem = Config.load_checked(args.config)
    cfg = apply_overrides(cfg, args)
    log_buffer = setup_logging(cfg, args)
    log.info("whispa %s starting from %s", __version__, Path(__file__).parent)
    if load_problem:
        log.error("%s", load_problem)

    from .autostart import AutostartManager

    autostart = AutostartManager()

    if args.autostart:
        if not autostart.available:
            print("autostart is only supported on Windows", file=sys.stderr)
            return 5
        if args.autostart == "on":
            ok = autostart.enable()
            print("autostart enabled" if ok else "could not enable autostart")
            return 0 if ok else 5
        if args.autostart == "off":
            ok = autostart.disable()
            print("autostart disabled" if ok else "could not disable autostart")
            return 0 if ok else 5
        enabled = autostart.is_enabled()
        print(f"autostart: {'on' if enabled else 'off'}")
        if enabled and autostart.is_stale():
            print("  (points at a different folder; it will be repaired on next start)")
        print(f"  command: {autostart.command}")
        return 0

    if args.list_devices:
        from .audio import list_input_devices

        for dev in list_input_devices():
            print(f"[{dev['index']:>2}] {dev['name']}  ({dev['channels']}ch)")
        return 0

    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
            log.error("config error: %s", problem)
        return 2

    if cfg.auto_update:
        try:
            from .update import check_and_apply, relaunch

            if check_and_apply():
                relaunch()
                return 0  # pragma: no cover - execv replaces this process
        except Exception:
            log.warning("auto-update check failed", exc_info=True)

    from .audio import MicRecorder
    from .learn import CorrectionLearner
    from .observe import CorrectionWatcher, make_observer

    recorder = MicRecorder(
        sample_rate=cfg.sample_rate,
        device=cfg.input_device,
        max_seconds=cfg.max_recording_seconds,
    )
    learner = CorrectionLearner(
        path=default_config_dir() / "learned.json",
        min_count=cfg.learn_min_count,
    )
    transcriber = WhisperTranscriber(
        model=cfg.model,
        device=cfg.device,
        compute_type=cfg.compute_type,
        cpu_threads=cfg.cpu_threads,
        language=cfg.language,
        beam_size=cfg.beam_size,
        vad_filter=cfg.vad_filter,
        # Learnt vocabulary biases the decoder, so repeat mistakes are
        # prevented rather than corrected after the fact.
        initial_prompt=(
            learner.prompt_bias(cfg.initial_prompt)
            if cfg.learn_bias_prompt
            else cfg.initial_prompt
        ),
        sample_rate=cfg.sample_rate,
        paragraph_pause_seconds=cfg.paragraph_pause_seconds,
        comma_pause_seconds=cfg.comma_pause_seconds,
    )

    def refresh_prompt() -> None:
        # Learnt vocabulary reaches the decoder as soon as it is learnt, not
        # at the next restart - otherwise the second correction of the same
        # word never had a chance to be prevented.
        if cfg.learn_bias_prompt:
            transcriber.initial_prompt = learner.prompt_bias(cfg.initial_prompt) or None

    def learn_from_edit(original: str, corrected: str) -> None:
        if learner.observe(original, corrected):
            refresh_prompt()

    watcher = None
    if cfg.learn_from_edits:
        watcher = CorrectionWatcher(
            observer=make_observer(True),
            on_correction=learn_from_edit,
            settle=cfg.learn_settle_seconds,
            window=cfg.learn_watch_seconds,
        )

    injector = (
        NullInjector()
        if args.dry_run
        else KeyboardInjector(
            method=cfg.inject_method,
            type_delay=cfg.type_delay,
            restore_clipboard=cfg.restore_clipboard,
            modifier_release_timeout=cfg.modifier_release_timeout,
        )
    )

    shutdown = threading.Event()

    overlay = None
    if cfg.overlay:
        try:
            from .overlay import Overlay

            overlay = Overlay(
                level_source=lambda: recorder.level,
                always_visible=cfg.overlay_always_visible,
            )
        except Exception:
            log.warning("on-screen indicator unavailable", exc_info=True)

    # Both defined before anything closes over them.
    tray_ref: list = [None]
    hotkeys: list = []

    engine = DictationEngine(
        recorder=recorder,
        transcriber=transcriber,
        injector=injector,
        sample_rate=cfg.sample_rate,
        min_seconds=cfg.min_recording_seconds,
        replacements=cfg.replacements,
        trailing_space=cfg.trailing_space,
        formatter=make_formatter(cfg),
        learner=learner,
        watcher=watcher,
        on_learned=refresh_prompt,
        on_state=lambda state, detail: _on_state(state, detail, overlay, tray_ref),
    )

    def quit_everything() -> None:
        shutdown.set()
        if overlay is not None:
            overlay.stop()

    log_window_ref: list = [None]

    def open_console() -> None:
        """Open the debug console on the tkinter thread."""
        if overlay is None:
            log.warning("the debug console needs the on-screen indicator enabled")
            return
        from .console import LogWindow

        def _open():
            if log_window_ref[0] is None:
                log_window_ref[0] = LogWindow(
                    log_buffer, log_path=default_config_dir() / "whispa.log"
                )
            log_window_ref[0].open(parent=overlay._root)

        overlay.call_soon(_open)

    def fix_last() -> None:
        """Open the correction dialog on the tkinter thread."""
        if overlay is None or not engine.last_injected:
            return
        from .dialog import show_correction_dialog

        overlay.call_soon(
            lambda: show_correction_dialog(
                engine.last_injected or "",
                on_corrected=lambda corrected: engine.teach(corrected),
                parent=overlay._root,
            )
        )

    def set_hotkey(spec: str) -> None:
        """Rebind the shortcut from the tray, and remember it.

        Rebinding first: if the spec is unusable the exception reaches the tray
        before anything has been written, so a bad choice cannot leave a config
        file that stops whispa starting next time.
        """
        if hotkeys:
            hotkeys[0].rebind(spec)
        cfg.hotkey = spec
        try:
            cfg.save(args.config)
        except OSError:
            log.exception("shortcut changed but could not be saved")
        log.info("shortcut is now %s", spec)

    def update_now() -> str:
        """The tray's "Update now": pull, and restart into the new version.

        Deliberately ignores cfg.auto_update - that flag governs the silent
        check at startup, and clicking the button is as explicit as consent
        gets. Restarting is spawn-then-quit rather than exec: the overlay,
        tray and hotkey listener are all live, so the clean path out is the
        normal shutdown with a fresh process already on its way up.
        """
        from .update import check_and_apply, spawn_replacement

        if not check_and_apply():
            return "already up to date (log has details if that seems wrong)"
        if spawn_replacement():
            quit_everything()
            return "updated - restarting"
        return "updated - restart whispa to finish"

    tray = None
    if not args.no_tray:
        try:
            from .tray import TrayIcon

            tray = TrayIcon(
                hotkey=cfg.hotkey,
                model=cfg.model,
                mode=cfg.hotkey_mode,
                on_quit=quit_everything,
                on_fix_last=fix_last if overlay is not None else None,
                learned_stats=lambda: learner.stats,
                on_open_console=open_console if overlay is not None else None,
                autostart=autostart,
                on_set_hotkey=set_hotkey,
                on_update_now=update_now,
            )
            tray_ref[0] = tray
        except Exception:
            log.warning("tray unavailable, continuing without it", exc_info=True)

    engine.start()

    def startup() -> None:
        """Load the model, then arm the hotkey. Runs off the UI thread."""
        if overlay is not None and load_problem:
            # No console to print to, so a broken config has to be visible here.
            overlay.set_state(State.ERROR, "config ignored - see log", force=True)
            time.sleep(2.5)
        if overlay is not None:
            overlay.set_state(State.TRANSCRIBING, f"loading {cfg.model}", force=True)
        try:
            if cfg.warmup:
                transcriber.warmup()
            else:
                transcriber.load()
        except Exception as exc:
            log.exception("could not load the model")
            if overlay is not None:
                overlay.set_state(State.ERROR, f"model failed: {exc}", force=True)
            return
        try:
            hotkey = GlobalHotkey(
                spec=cfg.hotkey,
                mode=cfg.hotkey_mode,
                on_start=engine.begin_recording,
                on_stop=engine.end_recording,
                on_held_change=lambda held: setattr(injector, "held_modifiers", held),
                tap_seconds=cfg.tap_seconds,
            )
            hotkey.start()
            hotkeys.append(hotkey)
        except Exception as exc:
            log.exception("could not register the global hotkey")
            if overlay is not None:
                overlay.set_state(State.ERROR, f"hotkey failed: {exc}", force=True)
            return
        # If the folder has been moved, the Run entry points somewhere stale;
        # fix it now rather than failing silently at next login.
        try:
            autostart.repair_if_stale()
        except Exception:
            log.debug("autostart repair check failed", exc_info=True)
        learning = "on" if (watcher and watcher.available) else "manual"
        log.info(
            "ready: %s (%s), learning %s", cfg.hotkey, cfg.hotkey_mode, learning
        )
        if overlay is not None:
            overlay.set_state(State.IDLE, "ready", force=False)

    threading.Thread(target=startup, name="whispa-startup", daemon=True).start()

    if tray is not None:
        try:
            tray.run_detached()
        except Exception:
            log.warning("tray could not run detached; using a thread", exc_info=True)
            threading.Thread(target=tray.run, daemon=True).start()

    try:
        signal.signal(signal.SIGINT, lambda *_: quit_everything())
    except ValueError:
        pass  # not on the main thread, e.g. under a test harness

    try:
        if overlay is not None:
            # tkinter insists on the main thread; the tray, the hotkey listener
            # and the transcription worker all have their own.
            overlay.run()
        else:
            shutdown.wait()
    finally:
        shutdown.set()
        for hk in hotkeys:
            hk.stop()
        if watcher is not None:
            watcher.cancel()
        engine.shutdown()
        if tray is not None:
            tray.stop()
        if log_window_ref[0] is not None:
            log_window_ref[0].close()
        learner.save()
        log.info("stopped: %s", engine.stats.summary())
        if args.console:
            print("\n" + engine.stats.summary())
    return 0


def _on_state(state: State, detail: str, overlay, tray_ref) -> None:
    if state is State.ERROR:
        log.error("%s", detail or "error")
    elif state is State.IDLE and detail:
        log.info("-> %s", detail)
    if overlay is not None:
        overlay.set_state(state, detail)
    tray = tray_ref[0] if tray_ref else None
    if tray is not None:
        tray.set_state(state, detail)


if __name__ == "__main__":
    raise SystemExit(main())

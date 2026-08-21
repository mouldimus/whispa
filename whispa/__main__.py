"""Entry point:  python -m whispa  [--options]"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from .app import DictationEngine, State
from .config import Config, default_config_dir
from .hotkey import GlobalHotkey
from .inject import KeyboardInjector, NullInjector
from .transcribe import WhisperTranscriber

log = logging.getLogger("whispa")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="whispa",
        description="Hold a key, speak, release - the text lands in whatever "
        "window has focus. Runs entirely offline.",
    )
    p.add_argument("--config", type=Path, help="path to config.json")
    p.add_argument("--hotkey", help="e.g. f9, scroll_lock, <ctrl>+<alt>+d")
    p.add_argument("--mode", choices=["hold", "toggle"], help="push-to-talk or toggle")
    p.add_argument("--model", help="base.en, small.en, medium.en, large-v3 ...")
    p.add_argument("--device", help="cpu or cuda")
    p.add_argument("--compute-type", help="int8, int8_float16, float16, float32")
    p.add_argument("--input-device", type=int, help="microphone index")
    p.add_argument(
        "--inject", choices=["paste", "type", "clipboard"], help="how to deliver text"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="transcribe and print, but do not touch the keyboard",
    )
    p.add_argument("--no-tray", action="store_true", help="run headless in the console")
    p.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    p.add_argument("--write-config", action="store_true", help="write defaults and exit")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    for attr, field_name in (
        ("hotkey", "hotkey"),
        ("mode", "hotkey_mode"),
        ("model", "model"),
        ("device", "device"),
        ("compute_type", "compute_type"),
        ("input_device", "input_device"),
        ("inject", "inject_method"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, field_name, value)
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.write_config:
        path = Config().save(args.config)
        print(f"wrote defaults to {path}")
        return 0

    if args.list_devices:
        from .audio import list_input_devices

        for dev in list_input_devices():
            print(f"[{dev['index']:>2}] {dev['name']}  ({dev['channels']}ch)")
        return 0

    cfg = apply_overrides(Config.load(args.config), args)
    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        return 2

    transcriber = WhisperTranscriber(
        model=cfg.model,
        device=cfg.device,
        compute_type=cfg.compute_type,
        cpu_threads=cfg.cpu_threads,
        language=cfg.language,
        beam_size=cfg.beam_size,
        vad_filter=cfg.vad_filter,
        initial_prompt=cfg.initial_prompt,
        sample_rate=cfg.sample_rate,
    )

    if args.dry_run:
        injector = NullInjector()
    else:
        injector = KeyboardInjector(
            method=cfg.inject_method,
            type_delay=cfg.type_delay,
            restore_clipboard=cfg.restore_clipboard,
            modifier_release_timeout=cfg.modifier_release_timeout,
        )

    from .audio import MicRecorder

    recorder = MicRecorder(
        sample_rate=cfg.sample_rate,
        device=cfg.input_device,
        max_seconds=cfg.max_recording_seconds,
    )

    # Defined before the tray, whose Quit item closes over it.
    shutdown = threading.Event()

    tray = None
    if not args.no_tray:
        try:
            from .tray import TrayIcon

            tray = TrayIcon(cfg.hotkey, cfg.model, on_quit=lambda: shutdown.set())
        except Exception:
            log.warning("tray unavailable, continuing without it", exc_info=args.verbose)

    def on_state(state: State, detail: str) -> None:
        if state is State.RECORDING:
            log.info("listening...")
        elif state is State.ERROR:
            log.error("%s", detail or "error")
        elif state is State.IDLE and detail:
            log.info("-> %s", detail)
        if tray is not None:
            tray.set_state(state, detail)

    engine = DictationEngine(
        recorder=recorder,
        transcriber=transcriber,
        injector=injector,
        sample_rate=cfg.sample_rate,
        min_seconds=cfg.min_recording_seconds,
        replacements=cfg.replacements,
        trailing_space=cfg.trailing_space,
        on_state=on_state,
    )
    engine.start()

    print(f"whispa: loading {cfg.model!r} (first run downloads the model)...")
    try:
        if cfg.warmup:
            transcriber.warmup()
        else:
            transcriber.load()
    except Exception as exc:
        print(f"could not load the model: {exc}", file=sys.stderr)
        return 3

    hotkey = GlobalHotkey(
        spec=cfg.hotkey,
        mode=cfg.hotkey_mode,
        on_start=engine.begin_recording,
        on_stop=engine.end_recording,
        # Let the injector see which modifiers are physically down, so it can
        # wait for them rather than pasting into a modified keystroke.
        on_held_change=lambda held: setattr(injector, "held_modifiers", held),
    )
    try:
        hotkey.start()
    except Exception as exc:
        print(f"could not register the global hotkey: {exc}", file=sys.stderr)
        return 4

    verb = "Hold" if cfg.hotkey_mode == "hold" else "Press"
    print(f"whispa ready. {verb} [{cfg.hotkey}] and speak. Ctrl-C to quit.")
    if args.dry_run:
        print("(dry run: text is printed, not typed)")

    signal.signal(signal.SIGINT, lambda *_: shutdown.set())

    try:
        if tray is not None:
            # pystray owns the main thread on Windows; the hotkey listener and
            # the transcription worker are already on their own threads.
            threading.Thread(
                target=lambda: (shutdown.wait(), tray.stop()), daemon=True
            ).start()
            tray.run()
        else:
            shutdown.wait()
    finally:
        hotkey.stop()
        engine.shutdown()
        if tray is not None:
            tray.stop()
        print("\n" + engine.stats.summary())
        cfg_dir = default_config_dir()
        log.debug("config dir was %s", cfg_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

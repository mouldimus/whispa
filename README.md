# whispa

Hold a key, speak, let go — the text appears in whatever window has focus.
A Wispr Flow / Whispa-style dictation tool that runs **entirely on your own
machine**: no API key, no account, no audio leaving the PC. After the one-time
model download it works with the network off.

---

## Install (Windows)

1. Install **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/windows/) —
   tick **"Add Python to PATH"** during setup.
2. Copy this whole `whispa` folder somewhere permanent, e.g. `C:\Tools\whispa`.
3. Double-click **`install.bat`**. It builds a virtual environment and pulls in
   the dependencies (~200 MB, once).
4. Double-click **`whispa.bat`**. The first launch downloads the speech model
   (~150 MB for `base.en`) and then prints `whispa ready`.

**Hold `F9`, speak, release.** The transcript is typed into the focused field.

To start it automatically at login, put a shortcut to `whispa-silent.vbs` in
the folder that opens when you run `shell:startup` in the Windows Run box.

---

## Everyday use

| Action | Key |
|---|---|
| Dictate | hold **F9**, speak, release |
| Quit | tray icon → **Quit**, or `Ctrl-C` in the console |

The tray dot shows what is happening: **grey** idle, **red** recording,
**amber** transcribing, **magenta** something went wrong.

Recordings shorter than 0.35 s are ignored as mis-presses, and a recording that
is entirely silent tells you the microphone is probably muted instead of quietly
doing nothing.

---

## Options

```
python -m whispa --help

  --hotkey f9              f9, scroll_lock, <ctrl>+<alt>+d ...
  --mode hold|toggle       push-to-talk, or press once to start / again to stop
  --model base.en          tiny.en, base.en, small.en, medium.en, large-v3
  --inject paste|type|clipboard
  --input-device 3         pick a microphone (see --list-devices)
  --dry-run                transcribe and print, never touch the keyboard
  --no-tray                run in the console only
  --list-devices           list microphones and exit
  --write-config           write a config.json full of defaults, then exit
```

Run `whispa.bat --write-config` to create the settings file, then edit it at
`%APPDATA%\whispa\config.json`. Useful knobs:

- `hotkey` / `hotkey_mode` — the trigger, and hold-vs-toggle.
- `model` — accuracy vs speed, see the table below.
- `inject_method` — `paste` (default, fast, works nearly everywhere),
  `type` (slower, for apps that block programmatic paste), or `clipboard`
  (copy only; you press Ctrl-V yourself).
- `replacements` — a dictionary applied to every transcript, e.g.
  `{"gonna": "going to", "kubernetes": "Kubernetes"}`.
- `initial_prompt` — a sentence of context that biases spelling. Putting your
  jargon and product names here is the cheapest accuracy win available.

### Why the default hotkey is a single key

A chord like `Ctrl+Alt+D` means that at the instant you release it to end the
recording, `Ctrl` and `Alt` may still be physically down — and the app is about
to send `Ctrl+V`. That turns into a modified keystroke and a mystery bug.
`F9` has no such problem. Chords still work, and the app waits for held
modifiers to come up before injecting, but a single key is simply more robust.

---

## Choosing a model

All models are English-only (`.en`) and quantised to `int8`, which is the right
trade on a CPU. Bigger models are more accurate on proper nouns and accents.

| Model | Speed | Size | Verdict on the test clip |
|---|---|---|---|
| `tiny.en` | 15.7x realtime | ~75 MB | Dropped punctuation and misheard a word — usable only if speed matters more than accuracy |
| **`base.en`** (default) | **9.5x realtime** | ~150 MB | Word-perfect with correct punctuation. The right default |
| `small.en` | 3.5x realtime | ~490 MB | Also word-perfect here; pulls ahead on accents, jargon and noisy rooms |

*Measured on a 2-core AMD EPYC VM with an 11-second clip of clear speech, warm
model, `int8`. **Your Windows PC will differ** — treat these as ratios between
models, not absolute promises. "9.5x realtime" means a 10-second sentence takes
about a second to transcribe.*

The first transcription after launch is slower while caches fill; the app runs a
throwaway decode at startup (`warmup: true`) so you don't feel it.

---

## Troubleshooting

**Nothing happens when I press F9.**
Another app may already own that key. Try `whispa.bat --hotkey scroll_lock`.
If you launched from an elevated (admin) window, note that Windows will not
deliver keystrokes from normal apps to an elevated one, and vice versa — run
whispa at the same privilege level as the app you are dictating into.

**It records but nothing is typed.**
Run `whispa.bat --dry-run` and speak. If the text prints in the console, the
transcription is fine and the problem is injection — try
`whispa.bat --inject type`, which synthesises keystrokes instead of pasting.

**"recording was silent — is the microphone muted?"**
Windows privacy settings block microphone access per-app. Check
*Settings → Privacy & security → Microphone*, and confirm the right input is
selected with `whispa.bat --list-devices` then `--input-device N`.

**The transcript is right but the words are wrong for my domain.**
Put your vocabulary in `initial_prompt`, and exact fixes in `replacements`.

**It types `Thank you.` when I say nothing.**
It shouldn't — Whisper emits a handful of stock phrases when fed silence, and
those are filtered out when they are the entire result. If you see one slip
through, add it to `_HALLUCINATION_ON_SILENCE` in `whispa/transcribe.py`.

---

## How it fits together

```
F9 down ──► hotkey.py ──► app.py ──► audio.py      (sounddevice → float32)
                             │
F9 up ────► hotkey.py ──► app.py ──► transcribe.py (faster-whisper, VAD)
                             │
                             └─────► inject.py     (clipboard + Ctrl-V)
```

`app.py` runs transcription on its own worker thread — blocking the hotkey
thread would freeze every key on the system — and uses a single queue so that
if you dictate twice quickly, the text still arrives in the order you spoke it.

| File | Job |
|---|---|
| `whispa/config.py` | Settings dataclass, JSON load/save, validation |
| `whispa/audio.py` | Microphone capture |
| `whispa/transcribe.py` | faster-whisper wrapper, text cleanup |
| `whispa/hotkey.py` | Global hotkey; match logic kept pure and testable |
| `whispa/inject.py` | Clipboard-paste and keystroke injection |
| `whispa/app.py` | State machine tying it together |
| `whispa/tray.py` | Tray icon |
| `whispa/__main__.py` | CLI entry point |

---

## What has actually been tested

This was written on a headless Linux VPS with no microphone, no keyboard and no
Windows, so be clear about which claims are backed by a run:

**Verified here, for real:**
- The full pipeline — `WhisperTranscriber` → `DictationEngine` → injector — on a
  real 11-second speech recording with the real `base.en` model, producing a
  word-perfect transcript (`tests/test_pipeline_real.py`).
- 42 unit tests, all passing. They cover hotkey matching (hold, toggle, chords,
  key auto-repeat, left/right modifier variants), config round-tripping and
  validation, text cleanup, silence-hallucination filtering, and the engine's
  edge cases: short presses, silent audio, double-start, microphone failure and
  ordering across queued utterances (`tests/test_units.py`); plus the injector's
  clipboard save/restore ordering and its wait-for-held-modifiers behaviour,
  including the stuck-modifier timeout (`tests/test_injector.py`).
- The CLI itself: `--help`, `--write-config`, and that an invalid config is
  rejected with a readable message and exit code 2.
- The model speed figures in the table above.

**Not verifiable from here — please check on your PC:**
- The global hotkey actually firing system-wide on Windows.
- Text injection into real applications (browser, editor, Slack).
- The tray icon rendering and its Quit item.
- Microphone capture through `sounddevice` on real hardware.

Run the tests yourself with:

```
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

The real-audio test needs a sample that is not in this repo; fetch it with:

```
curl -L -o tests/data/jfk.flac https://github.com/SYSTRAN/faster-whisper/raw/master/tests/data/jfk.flac
.venv\Scripts\python.exe tests/test_pipeline_real.py
```

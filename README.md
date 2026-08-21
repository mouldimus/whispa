# whispa

Tap or hold a key, speak — the text appears in whatever window has focus.
A Wispr Flow-style dictation tool that runs **entirely on your own machine**:
no API key, no account, no audio leaving the PC. After the one-time model
download it works with the network off.

It also **learns**. When you fix a word it got wrong, it notices, and stops
getting that word wrong.

---

## Install (Windows)

1. Copy this `whispa` folder somewhere permanent, e.g. `C:\Tools\whispa`.
2. Double-click **`install.bat`**.
3. Double-click **`whispa.bat`**.

That's it — **you do not need to install Python first.** If `install.bat` can't
find Python 3.11 or newer it offers to install Python 3.12 for you, using
`winget` if available and the official python.org installer otherwise. It
installs per-user, so no administrator rights are needed. Then it builds the
virtual environment and pulls in the dependencies (~200 MB, once).

If Python was just installed, the setup window may need to be closed and
`install.bat` run once more — a fresh window is needed to pick up the new PATH.
It tells you if so.

`whispa.bat` opens no console window; it goes straight to the system tray. The
on-screen pill shows `loading base.en` while the model downloads (~150 MB,
first run only), then `ready`.

To have it running from login, use the tray's **Settings → Start with Windows**.

---

## Dictating

**Tap `F9`** — recording latches on. Speak. **Tap `F9` again** to finish.
**Or hold `F9` down**, speak, and release. Same key; it works out which you
meant from how long you held it.

That is the default `hybrid` mode. A press shorter than 0.35 s is a tap; longer
is push-to-talk. Set `hotkey_mode` to `hold` or `toggle` if you want only one
of the two.

### The indicator

A small pill sits near the bottom of the screen and tells you exactly where
things are:

| Look | Meaning |
|---|---|
| Hidden | Idle. Nothing is being recorded |
| Grey dot + `ready` | Running and armed (pin it with `--show-overlay-always`) |
| **Red dot + moving bars** | Recording, and the bars are your actual microphone level |
| Red dot + flat grey bars + `no signal` | Recording but **hearing nothing** — muted or wrong input device |
| **Amber pulse + `thinking`** | Transcribing |
| Green flash + the text | Done — that's what was typed |
| Magenta | Something failed; details in the log |

The bars are computed from the real input signal, so they answer "is it
actually picking me up?" rather than just animating reassuringly.

The tray icon mirrors the same states, and its menu has **Fix last
dictation...**, the model in use, how much has been learnt, and a **Settings**
submenu.

### Settings (tray menu)

**Open debug console...** — a window showing whispa's log. It opens with the
history already in it, not just whatever happens next, because by the time you
notice a problem the interesting lines have usually already gone by. It
follows new lines live, colours warnings and errors, and has a *Copy all*
button for when you want to paste the log somewhere. The file it mirrors is
`%APPDATA%\whispa\whispa.log`.

**Start with Windows** — a checkbox. On, whispa starts at login; off, it
doesn't. It writes a single value to your own `HKCU\...\CurrentVersion\Run`
key, so it needs no admin rights and turning it off removes it completely. If
you move the whispa folder later, it notices the entry points at the old place
and re-points it on next start rather than quietly failing every morning.

Both are also available without the tray:

```
whispa-console.bat --autostart on      (or off, or status)
```

---

## Learning from your corrections

Dictate `send it to Jon`, change it to `send it to John`, and whispa records
that. Do it a second time and it starts making the fix itself — and adds
"John" to the vocabulary hint it gives the speech model, so it is more likely
to get it right in the first place rather than patching it afterwards.

Two things it deliberately will **not** do:

- **Act on a single edit.** One change could be you changing your mind. A
  correction has to repeat before it is trusted (`learn_min_count`, default 2).
- **Learn from rewrites.** If you dictate a sentence and then reword it, that
  says nothing about what was misheard. Edits that change most of the words
  are ignored.

**How it sees your edits.** A few seconds after typing, it reads the focused
control back through Windows UI Automation and compares. That works in most
modern apps — browsers, Office, Electron apps like Slack and VS Code — and not
in some others. Where it doesn't work, use the tray's **Fix last dictation...**
dialog: it shows what was typed, you correct it, and the lesson is identical.
Manual corrections are trusted immediately, since you clearly meant them.

Learned corrections live in `%APPDATA%\whispa\learned.json`. Delete it to
forget everything; edit it to curate.

Turn the whole thing off with `--no-learn` or `learn_from_edits: false`.

---

## Options

```
whispa-console.bat --help

  --hotkey f9              f9, scroll_lock, <ctrl>+<alt>+d ...
  --mode hybrid|hold|toggle
  --model base.en          tiny.en, base.en, small.en, medium.en, large-v3
  --inject paste|type|clipboard
  --input-device 3         pick a microphone (see --list-devices)
  --dry-run                transcribe and log, never touch the keyboard
  --no-overlay             hide the on-screen indicator
  --show-overlay-always    keep the indicator visible when idle
  --no-learn               don't learn from edits
  --no-tray                no tray icon
  --console                keep a console window and log to it
  --list-devices           list microphones and exit
  --write-config           write a config.json full of defaults, then exit
```

Run `whispa-console.bat --write-config` to create the settings file, then edit
`%APPDATA%\whispa\config.json`. Worth knowing:

- `hotkey`, `hotkey_mode`, `tap_seconds` — the trigger and its feel.
- `model` — accuracy vs speed, see the table below.
- `inject_method` — `paste` (default, fast, works nearly everywhere),
  `type` (slower, for apps that block programmatic paste), or `clipboard`.
- `replacements` — hand-written fixes applied to every transcript, e.g.
  `{"gonna": "going to"}`. Learned corrections stack on top of these.
- `initial_prompt` — a sentence of context that biases spelling. Your jargon
  and product names here is the cheapest accuracy win available; learned
  vocabulary is appended to it automatically.

If a config file is corrupt, whispa starts on defaults and says so in the log
and the indicator rather than failing to appear.

### Why the default hotkey is a single key

A chord like `Ctrl+Alt+D` means that when you release it to end the recording,
`Ctrl` and `Alt` may still be physically down — and the app is about to send
`Ctrl+V`. That becomes a modified keystroke and a mystery bug. `F9` has no such
problem. Chords still work, and injection waits for held modifiers to come up
first, but a single key is simply more robust.

---

## Choosing a model

All English-only (`.en`), quantised to `int8`, which is the right trade on a CPU.

| Model | Speed | Size | Verdict on the test clip |
|---|---|---|---|
| `tiny.en` | 15.7x realtime | ~75 MB | Dropped punctuation and misheard a word — only if speed beats accuracy |
| **`base.en`** (default) | **9.5x realtime** | ~150 MB | Word-perfect with correct punctuation. The right default |
| `small.en` | 3.5x realtime | ~490 MB | Also word-perfect here; pulls ahead on accents, jargon and noisy rooms |

*Measured on a 2-core AMD EPYC VM with an 11-second clip of clear speech, warm
model. **Your PC will differ** — treat these as ratios between models, not
absolute promises. "9.5x realtime" means a 10-second sentence takes about a
second.* The first transcription after launch is slower while caches fill, so
a throwaway decode runs at startup (`warmup: true`) to absorb it.

---

## Troubleshooting

Since there is no console window, **the log is the first place to look**:
`%APPDATA%\whispa\whispa.log`. Or run `whispa-console.bat` to watch it live.

**Nothing happens at all.** Run `whispa-console.bat` — it keeps the window open
and prints the reason. Most often another app owns F9; try `--hotkey scroll_lock`.
Note that Windows will not deliver keystrokes between apps at different
privilege levels, so run whispa the same way as the app you dictate into.

**The bars stay flat and say `no signal`.** It is recording but hearing
nothing. Check *Settings → Privacy & security → Microphone*, then
`whispa-console.bat --list-devices` and `--input-device N`.

**It records but nothing is typed.** Run `whispa-console.bat --dry-run` and
speak. If the text appears in the console, transcription is fine and the
problem is injection — try `--inject type`.

**It isn't learning.** The tray menu shows the counts. If it says `0 tracked`
after several corrections, UI Automation can't read that app; use **Fix last
dictation...** instead. Remember a correction must repeat twice before it is
applied.

**The indicator is in the way.** `--no-overlay`, or edit `overlay: false`.

---

## How it fits together

```
F9 ──► hotkey.py ──► app.py ──► audio.py       (sounddevice → float32 + level)
                        │
                        ├─────► transcribe.py  (faster-whisper, VAD)
                        │
                        ├─────► inject.py      (clipboard + Ctrl-V)
                        │
                        └─────► observe.py ──► learn.py   (read back, diff, learn)

overlay.py (tkinter, main thread)   tray.py (pystray, own thread)
```

Transcription runs on its own worker thread — blocking the hotkey thread would
freeze every key on the system — behind a single queue, so dictating twice
quickly still types in the order you spoke. The overlay owns the main thread
because tkinter insists; the tray runs detached.

| File | Job |
|---|---|
| `whispa/config.py` | Settings, JSON load/save, validation, corrupt-file fallback |
| `whispa/audio.py` | Microphone capture and live level metering |
| `whispa/transcribe.py` | faster-whisper wrapper, text cleanup |
| `whispa/hotkey.py` | Global hotkey; hybrid/hold/toggle logic, kept pure |
| `whispa/inject.py` | Clipboard-paste and keystroke injection |
| `whispa/observe.py` | Reads the focused control back to spot your edits |
| `whispa/learn.py` | Turns edits into corrections and vocabulary |
| `whispa/app.py` | State machine tying it together |
| `whispa/autostart.py` | The "Start with Windows" registry toggle |
| `whispa/console.py` | Log ring buffer and the debug console window |
| `whispa/overlay.py` | The on-screen pill |
| `whispa/dialog.py` | "Fix last dictation" dialog |
| `whispa/tray.py` | Tray icon |
| `whispa/__main__.py` | CLI entry point and startup sequencing |

---

## What has actually been tested

Written on a headless Linux VPS with no microphone, no keyboard, no display and
no Windows, so it is worth being precise about which claims are backed by a run.

**Verified for real:**
- The full pipeline on a real 11-second speech recording with the real
  `base.en` model → word-perfect transcript.
- **The whole learning loop against real model output**: real transcript → a
  user edit → correction learnt → *re-transcribed the same real audio and
  confirmed the learnt fix was applied.*
- 129 unit tests. Hotkey matching including every hybrid tap/hold/latch
  transition with a fake clock; correction extraction and its refusal to learn
  from rewrites; span location under edits elsewhere in the document and in a
  30,000-character document; learner persistence, conflicts and thresholds;
  level-meter mapping (silence really does read as silence); indicator
  visibility rules; injector clipboard restore and held-modifier waiting;
  engine edge cases; corrupt-config fallback; the autostart toggle including
  stale-entry detection and repair after the folder moves; and the log ring
  buffer, including that a reader which falls behind never repeats or skips
  lines as old ones are evicted.
- Model speed figures in the table above.
- CLI: `--help`, `--write-config`, `--autostart`, invalid config handling.
- That the Python installer URLs `install.bat` builds for x64, ARM64 and x86
  all resolve, and that every branch in the script has a defined target.

**Not verifiable from here — worth an eye on your PC:**
- The global hotkey firing system-wide, and hybrid tap/hold feel in practice.
- Text injection into real applications.
- **The overlay and tray rendering at all** — there is no display on the build
  machine, so tkinter and pystray were never actually drawn. Their decision
  logic is tested; their pixels are not.
- UI Automation read-back in the specific apps you use.
- Microphone capture through `sounddevice` on real hardware.
- **`install.bat` has never been executed** — there is no Windows here. Its
  logic and its download URLs were checked, but the Python bootstrap itself is
  unproven until you run it on a PC without Python.
- Writing to the real Windows registry. The autostart logic is tested against
  an in-memory stand-in; `winreg` itself is not exercised here.

Run the tests yourself:

```
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

The real-audio test needs a sample not shipped in this repo:

```
curl -L -o tests/data/jfk.flac https://github.com/SYSTRAN/faster-whisper/raw/master/tests/data/jfk.flac
.venv\Scripts\python.exe tests/test_pipeline_real.py
```

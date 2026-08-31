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

`install.bat` also turns this folder into a checkout of whispa's git repo
(installing Git the same way it installs Python, if needed), which is what
lets it **update itself** afterwards — see below.

---

## Updating

whispa's source lives at [github.com/mouldimus/whispa](https://github.com/mouldimus/whispa).
Once a machine has been through `install.bat`, that copy checks for
a newer version every time it starts: if one is there, it pulls it, resyncs
dependencies only if `requirements.txt` changed, and restarts itself once,
silently — no console window, no prompt. Ship a fix by pushing to the repo;
every machine picks it up the next time someone dictates something.

If a machine is offline, has local edits sitting in the folder, or was never
converted to a git checkout, it just starts normally on whatever version is
already there — this is best-effort and never blocks startup. Turn it off
per-machine with `auto_update: false` in `config.json` if one machine needs to
stay pinned to a version.

There is also a button: **tray → Settings → Update now** pulls immediately
and, if anything new landed, restarts whispa into it — no waiting for the
next launch. The status line in the menu shows how it went. The button works
even when `auto_update` is off, since clicking it is as explicit as consent
gets.

---

## Dictating

**Tap `F9`** — recording latches on. Speak. **Tap `F9` again** to finish.
**Or hold `F9` down**, speak, and release. Same key; it works out which you
meant from how long you held it.

That is the default `hybrid` mode. A press shorter than 0.35 s is a tap; longer
is push-to-talk. Set `hotkey_mode` to `hold` or `toggle` if you want only one
of the two.

The key is not fixed: **tray → Settings → Shortcut** offers `F9`, `F8`, `F4`,
`Scroll Lock`, `Pause`, `Ctrl + Win (Start)`, `Ctrl + Shift + Space` and
`Ctrl + Alt + D`. Picking one rebinds it immediately — no restart — and writes
it to `config.json`. Anything pynput can name works if you would rather type it
into the config yourself.

### Paragraphs, bullets and formatting

Whisper returns one unbroken line, which is fine for a search box and useless
for a paragraph of writing. whispa breaks it up three ways:

**Pause where you would break.** Stop speaking for **two seconds** and the next
sentence starts a new paragraph. This is measured in the waveform, not taken
from whisper's timestamps — whisper stretches those across silence, so its own
transcript has no idea where you paused. Tune it with `paragraph_pause_seconds`
(lower = more paragraphs; below about 1.5 s it starts breaking mid-thought,
because ordinary rhetorical pauses run 1.0–1.5 s). Set `paragraph_style` to
`off` in chat apps where a newline sends the message.

**Say what you want.** Spoken as their own phrase, these become structure:

| Say | You get |
|-----|---------|
| "new paragraph" / "next paragraph" | a blank line |
| "new line" / "next line" | a line break |
| "bullet point" / "new bullet" / "next point" | `- ` and the item |
| "numbered point" / "next number" | `1. `, `2. `, ... |

So *"shopping. bullet point milk. bullet point bread."* types

```
Shopping.
- Milk.
- Bread.
```

A command is only obeyed where it cannot be part of a sentence — at the start,
or straight after a `.` `,` `;` `:` `!` `?` — so "we opened a **new line** of
business" stays as words. Add your own (or drop one you keep saying by
accident) with `voice_command_extras`, e.g.
`{"full stop": ".", "next point": ""}`. `--no-voice-commands` turns the lot off.

**Sentences get capitals**, including the first word of each bullet and
paragraph. `auto_capitalise: false` if you would rather it didn't.

### Cleaning up how you actually talk

Real, unscripted speech is messier than a script: filler words, stutters, and
catching yourself mid-sentence. whispa cleans up three of these by default:

- **Filler words** — "um", "uh", "erm" are dropped outright.
- **Immediate repeats** — "the the meeting" becomes "the meeting". This is
  always treated as a stutter, even on the rare occasion it was deliberate
  emphasis ("very very good") — there is no way to tell the two apart from
  the text alone.
- **An explicit correction** — say "scratch that", "strike that", or
  "disregard that" and whispa drops everything back to the last sentence (or
  the last "new paragraph"/"bullet point"/etc.) along with the cue phrase
  itself, keeping only what follows: *"meet me at three, scratch that, meet
  me at four"* types **"meet me at four."** The cue list is deliberately this
  short — a false positive here silently deletes real words, which is worse
  than leaving a false start in the transcript for **Fix last dictation...**
  to clean up. Reaching back only works within the current sentence: a false
  start that got its own full stop ("Meet at three. Scratch that. Meet at
  four.") only loses the cue phrase, not the sentence before it. Milder,
  more ambiguous phrases like "no wait" or "no, not that" are deliberately
  *not* treated as corrections, since people say those as literal content
  too often for it to be safe.

Turn all three off with `remove_disfluencies: false`.

**A shorter pause also implies a comma — but only before a new clause.**
Measured against real audio, whisper reliably drops the comma it would
otherwise write once a pause gets past about a second. The catch, measured
against real interview speech: a *word-search* hesitation pauses just as
long mid-phrase ("and then back … to New Jersey"), where a comma is exactly
wrong. So the comma only lands when the pause is at least
`comma_pause_seconds` (default `0.8`) **and** the next word starts a new
clause — "and", "but", "so", "because", "which" and friends. On the test
material that gate removed every wrong insertion and kept every right one.
Set `comma_pause_seconds: 0` to turn it off.

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

**Shortcut** — the eight bindings listed above, as a radio list showing
which one is live. Changing it takes effect on the next keypress and survives a
restart. If the shortcut in your `config.json` isn't one of the eight, it stays
in the list as its own entry, so trying a preset is never a one-way door.

**Update now** — pulls the latest version from the repo immediately and, if
anything new landed, restarts whispa into it. See *Updating* above. The
status line at the top of the menu shows the outcome ("checking for
updates...", "updated - restarting", "already up to date").

**Start with Windows** — a checkbox. On, whispa starts at login; off, it
doesn't. It writes a single value to your own `HKCU\...\CurrentVersion\Run`
key, so it needs no admin rights and turning it off removes it completely. If
you move the whispa folder later, it notices the entry points at the old place
and re-points it on next start rather than quietly failing every morning.

The console and autostart are also available without the tray:

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
  --paragraphs blank|single|off
  --no-voice-commands      ignore "new paragraph", "bullet point", ....
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
- `paragraph_style`, `paragraph_pause_seconds`, `voice_commands`,
  `voice_command_extras`, `auto_capitalise`, `bullet_prefix`,
  `remove_disfluencies`, `comma_pause_seconds` — the formatting, described
  above.
- `replacements` — hand-written fixes applied to every transcript, e.g.
  `{"gonna": "going to"}`. Learned corrections stack on top of these.
- `initial_prompt` — a sentence of context that biases spelling. Your jargon
  and product names here is the cheapest accuracy win available; learned
  vocabulary is appended to it automatically.
- `auto_update` — pull updates from git on startup (default `true`). See
  *Updating* above.

If a config file is corrupt, whispa starts on defaults and says so in the log
and the indicator rather than failing to appear.

### Why the default hotkey is a single key

A chord like `Ctrl+Alt+D` means that when you release it to end the recording,
`Ctrl` and `Alt` may still be physically down — and the app is about to send
`Ctrl+V`. That becomes a modified keystroke and a mystery bug. `F9` has no such
problem. Chords still work, and injection waits for held modifiers to come up
first, but a single key is simply more robust.

`Ctrl + Win` has one extra hazard of its own: Windows opens the Start menu when
the Win key comes *up* without another key in between, and a Start menu that
steals focus mid-dictation means the transcript is typed into the search box.
whispa hides that key-press from Windows while the rest of the chord is held —
and only then, so `Win`, `Win+E` and the rest keep working — and taps an unused
virtual key when you press Win first, which is enough for Windows to treat it
as part of a combination. It is the one part of this that has never been run on
Windows here; if the Start menu still appears, say so and pick another
shortcut in the meantime.

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
| `whispa/update.py` | Git-based self-update, run at startup |
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
- **Paragraph detection on real audio**: the real speech sample with a 2.5 s
  silence spliced into the middle, through the real model and the real engine,
  produced exactly two paragraphs broken at the right word — and the same
  recording without the pause produced one, with the transcript unchanged. The
  first design (decoding each pause-separated piece on its own) was thrown away
  because that same test showed it degrading the transcript to "ASK NOT!"; the
  version that ships decodes once and cuts only the text, at a measured cost of
  nothing (1.25 s vs 1.29 s on the 12 s sample).
- **A two-pass "decode the words, then decode again to add punctuation" idea
  was tried and thrown away** the same way, for the same reason: measured
  against the real model, priming the second decode with the first pass's
  unpunctuated line made it copy that style, producing *worse* punctuation
  and casing than a single decode - it biases style, not just vocabulary.
- **The `comma_pause_seconds` measurement itself**: spliced pauses of
  0.4/0.7/1.0/1.5s into the same real recording and diffed the real model's
  output against the unspliced baseline. The comma after "Americans" survived
  a 0.4-0.7s gap and was dropped at 1.0s+, which is why the feature exists -
  and the same test also reproduced a genuine ~1.1s rhetorical pause already
  present in the *unspliced* recording with no comma warranted there at all,
  which is why the connective gate exists.
- **The whole formatting pipeline against ~6 minutes of real unscripted
  interview speech** (three archive.org oral-history recordings - three
  different speakers, decades apart in recording quality), decoded with the
  real model under four configurations and diffed. This is what proved the
  stutter collapse on genuine stutters ("we lived in, we lived in Trenton" ->
  "we lived in Trenton") and no-op behaviour on the fluent speaker; that
  ungated pause-commas mangle hesitant speech ("back, to New Jersey",
  "anybody, else's") even at 1.2s; and that the connective gate removes
  every one of those while keeping the correct insertions. It also showed
  `base.en` mostly drops "um"/"uh" on its own, so the filler stripping is
  insurance for models that don't.
- 212 unit tests. Hotkey matching including every hybrid tap/hold/latch
  transition with a fake clock; correction extraction and its refusal to learn
  from rewrites; span location under edits elsewhere in the document and in a
  30,000-character document; learner persistence, conflicts and thresholds;
  level-meter mapping (silence really does read as silence); indicator
  visibility rules; injector clipboard restore and held-modifier waiting;
  engine edge cases; corrupt-config fallback; the autostart toggle including
  stale-entry detection and repair after the folder moves; and the log ring
  buffer, including that a reader which falls behind never repeats or skips
  lines as old ones are evicted; the pause-to-comma-or-paragraph
  classification; and disfluency removal, including that a correction cannot
  reach back across a paragraph mark and that an ambiguous phrase like "no
  wait" is deliberately left alone.
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
- **The git bootstrap in `install.bat`** (installing Git via winget, turning
  an existing folder-copy into a checkout with `git init` + `checkout -f`) and
  **the self-update in `whispa/update.py`** (the git subprocess calls,
  `os.execv` relaunching `pythonw.exe` at startup, and the tray's Update now
  spawning a replacement via `whispa-silent.vbs` before quitting) — the
  update logic itself is unit-tested against a fake git and a fake process
  launcher, but none of it has run against real git or a real Windows
  process relaunch.
- Writing to the real Windows registry. The autostart logic is tested against
  an in-memory stand-in; `winreg` itself is not exercised here.
- **`remove_disfluencies` against whisper's actual output for real filler
  words and false starts** — real `base.en` decodes here confirmed the
  comma-dropping behaviour above, but the audio available had no "um"s or
  self-corrections in it to decode, so the text-cleanup rules themselves are
  unit-tested against hand-written strings, not a real transcript that
  contains one.
- **Hiding the Windows key from the Start menu** for the `Ctrl + Win` shortcut.
  The decision logic is unit-tested; the pynput key filter it drives is
  Windows-only and has never run. If the Start menu appears mid-dictation, that
  is where to look.

Run the tests yourself:

```
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

The real-audio test needs a sample not shipped in this repo:

```
curl -L -o tests/data/jfk.flac https://github.com/SYSTRAN/faster-whisper/raw/master/tests/data/jfk.flac
.venv\Scripts\python.exe tests/test_pipeline_real.py
```

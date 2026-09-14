"""Which microphone to record from.

PortAudio, underneath `sounddevice`, enumerates devices once when it starts
and never looks again. A headset plugged in after whispa launched does not
exist as far as it is concerned, and "the default input" stays whatever
Windows had selected at that moment, however many times you change it in the
sound settings since. The only way to see the current state is to shut
PortAudio down and bring it back up, which is what `refresh()` does. It takes
a fraction of a second, so it is done between recordings, never on the
hotkey press itself.

A microphone is chosen by a *spec*:

- `None` (or "") - the system default, re-checked every few seconds so that
  plugging in a headset, or picking a different device in Windows, takes
  effect without touching whispa.
- a name - matched against the device list at each check, so it survives the
  index reshuffle that happens whenever anything is plugged in or out.
- an index - as `--list-devices` prints it. Kept for hand-edited configs; a
  name is more robust.

Re-initialising PortAudio is not free of risk: it is documented as not
thread-safe, it must never overlap an open stream or even a device query, and
on Windows it re-enumerates every driver (WDM-KS included) each time. So it
happens as rarely as possible: only when `fingerprint()` - a cheap winmm
query that never touches PortAudio - says the set of devices or the default
has actually changed. And the default is followed without any re-init at
all: recording through the MME "Microsoft Sound Mapper" makes Windows pick
the current default at the moment the stream opens.

Deliberately free of numpy and of any module-level `sounddevice` import, so
that it can be unit-tested on a box with neither.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# The MME wave mapper, as PortAudio names it. Opening it asks winmm for the
# current default recording device, so it follows the Windows setting without
# PortAudio ever being told anything changed.
MAPPER_PREFIX = "microsoft sound mapper"


@dataclass(frozen=True)
class Choice:
    """What a spec resolved to."""

    # What to hand to sounddevice: an index, or None for its own default.
    index: int | None
    # The device's name as the OS reports it; "" if nothing could be found.
    name: str
    # Empty when the spec was honoured; otherwise why the default was used
    # instead (device unplugged, index out of range, ...).
    fallback: str = ""
    # True when this is "whatever Windows calls the default" rather than a
    # device picked by name or index - `index` is then the wave mapper, or
    # None where there is no mapper (which makes PortAudio use its own default).
    follows_default: bool = False

    def describe(self) -> str:
        if not self.name:
            return "no microphone found"
        return f"{self.name} (system default)" if self.follows_default else self.name


def _sd():
    import sounddevice as sd  # lazy: no audio stack in CI/tests

    return sd


def refresh() -> None:
    """Make PortAudio re-enumerate, so hot-plugged devices and a changed
    system default become visible.

    Must not run while a stream is open, or while any other thread is
    querying devices: PortAudio frees and rebuilds its device tables here,
    and a reader that lands in the middle of that is an access violation,
    not a Python exception. MicRecorder serialises every PortAudio call
    behind one lock for exactly this reason.
    """
    sd = _sd()
    try:
        sd._terminate()
    except Exception:
        log.debug("PortAudio terminate failed", exc_info=True)
    try:
        sd._initialize()
    except Exception:
        log.warning("PortAudio re-initialise failed", exc_info=True)


def _all_devices() -> list[dict[str, Any]]:
    sd = _sd()
    try:
        raw = sd.query_devices()
    except Exception:
        log.debug("could not query devices", exc_info=True)
        return []
    return [dict(dev) for dev in raw]


def default_input() -> dict[str, Any] | None:
    sd = _sd()
    try:
        dev = dict(sd.query_devices(kind="input"))
    except Exception:
        return None
    if dev.get("max_input_channels", 0) <= 0:
        return None
    if "index" not in dev:
        # Older sounddevice: find it by name.
        for idx, other in enumerate(_all_devices()):
            if other.get("name") == dev.get("name"):
                dev["index"] = idx
                break
    return dev


def list_input_devices(all_apis: bool = False) -> list[dict[str, Any]]:
    """Input devices as [{index, name, channels, hostapi, default}].

    Windows exposes every microphone through several host APIs (MME,
    DirectSound, WASAPI, WDM-KS) so the raw list shows each one three or four
    times, slightly renamed. By default this keeps only the host API the
    system default belongs to, which lists every device exactly once - the
    view Windows itself shows. `all_apis=True` gives the raw list.
    """
    default = default_input()
    default_index = default.get("index") if default else None
    default_api = default.get("hostapi") if default else None
    devices = []
    for idx, dev in enumerate(_all_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        if not all_apis and default_api is not None and dev.get("hostapi") != default_api:
            continue
        devices.append(
            {
                "index": dev.get("index", idx),
                "name": dev.get("name", "?"),
                "channels": dev.get("max_input_channels", 0),
                "hostapi": dev.get("hostapi"),
                "default": dev.get("index", idx) == default_index,
                "default_samplerate": dev.get("default_samplerate"),
            }
        )
    return devices


def _mapper_index(default: dict[str, Any] | None) -> int | None:
    """The wave mapper in the default's host API, or None if there is none
    (any non-Windows box, or a default that lives in another host API)."""
    if not default:
        return None
    for idx, dev in enumerate(_all_devices()):
        if (
            dev.get("hostapi") == default.get("hostapi")
            and dev.get("max_input_channels", 0) > 0
            and str(dev.get("name", "")).casefold().startswith(MAPPER_PREFIX)
        ):
            return dev.get("index", idx)
    return None


_winmm: Any = None


def _load_winmm() -> Any:
    """ctypes bindings for the three winmm calls fingerprint() needs."""
    global _winmm
    if _winmm is not None:
        return _winmm
    import ctypes
    from ctypes import wintypes

    class WAVEINCAPSW(ctypes.Structure):
        _fields_ = [
            ("wMid", wintypes.WORD),
            ("wPid", wintypes.WORD),
            ("vDriverVersion", wintypes.UINT),
            ("szPname", wintypes.WCHAR * 32),
            ("dwFormats", wintypes.DWORD),
            ("wChannels", wintypes.WORD),
            ("wReserved1", wintypes.WORD),
        ]

    lib = ctypes.WinDLL("winmm")
    lib.waveInGetNumDevs.argtypes = ()
    lib.waveInGetNumDevs.restype = wintypes.UINT
    lib.waveInGetDevCapsW.argtypes = (ctypes.c_size_t, ctypes.POINTER(WAVEINCAPSW), wintypes.UINT)
    lib.waveInGetDevCapsW.restype = wintypes.UINT
    lib.waveInMessage.argtypes = (ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p)
    lib.waveInMessage.restype = wintypes.UINT
    _winmm = (lib, WAVEINCAPSW)
    return _winmm


# winmm's "which device does the mapper currently prefer?" - the same query
# PortAudio uses to mark its default input, but callable at any time.
_WAVE_MAPPER = 0xFFFFFFFF
_DRVM_MAPPER_PREFERRED_GET = 0x2015


def fingerprint() -> tuple | None:
    """A cheap snapshot of the recording devices Windows has right now, and
    which one is the default - without touching PortAudio.

    Two equal fingerprints mean nothing worth a PortAudio re-init has
    happened. Returns None where it cannot tell (not Windows, winmm missing),
    and callers then fall back to re-initialising on a timer as before.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        lib, caps_type = _load_winmm()
        count = int(lib.waveInGetNumDevs())
        names = []
        for index in range(count):
            caps = caps_type()
            if lib.waveInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                names.append(caps.szPname)
            else:
                names.append(f"#{index}")
        preferred = wintypes.DWORD(_WAVE_MAPPER)
        status = wintypes.DWORD(0)
        result = lib.waveInMessage(
            ctypes.c_void_p(_WAVE_MAPPER),
            _DRVM_MAPPER_PREFERRED_GET,
            ctypes.addressof(preferred),
            ctypes.addressof(status),
        )
        default = int(preferred.value) if result == 0 else None
        return (count, default, tuple(names))
    except Exception:
        log.debug("device fingerprint unavailable", exc_info=True)
        return None


def is_auto(spec: Any) -> bool:
    return spec is None or (isinstance(spec, str) and not spec.strip())


def resolve(spec: Any) -> Choice:
    """Turn a spec into something sounddevice can open, right now.

    Falls back to the system default rather than failing: a missing headset
    should mean "recorded on the laptop mic", with a note in the log, not a
    dictation tool that silently stops working.
    """
    default = default_input()
    default_choice = Choice(
        _mapper_index(default), default.get("name", "") if default else "", follows_default=True
    )
    if is_auto(spec):
        return default_choice

    if isinstance(spec, str) and spec.strip().isdigit():
        spec = int(spec.strip())

    devices = list_input_devices(all_apis=True)
    if isinstance(spec, int) and not isinstance(spec, bool):
        for dev in devices:
            if dev["index"] == spec:
                return Choice(spec, dev["name"])
        return Choice(
            default_choice.index,
            default_choice.name,
            f"no input device at index {spec}; using the system default",
            follows_default=True,
        )

    wanted = str(spec).strip().casefold()
    exact = [d for d in devices if d["name"].casefold() == wanted]
    partial = [d for d in devices if wanted in d["name"].casefold()]
    matches = exact or partial
    if not matches:
        return Choice(
            default_choice.index,
            default_choice.name,
            f"microphone {spec!r} is not connected; using the system default",
            follows_default=True,
        )
    # Prefer the host API the default lives in - on Windows that is the one
    # whose names match what the user picked from the menu.
    default_api = default.get("hostapi") if default else None
    matches.sort(key=lambda d: (d.get("hostapi") != default_api, d["index"]))
    chosen = matches[0]
    return Choice(chosen["index"], chosen["name"])

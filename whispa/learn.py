"""Learning from the edits you make after dictating.

When whispa types "ask not what your country" and you change "ask" to "asked",
that edit is a free training signal: it says what the model heard and what you
actually meant. Collect enough of those and two things become possible -
substituting known mistakes automatically, and biasing the decoder toward your
vocabulary before it makes the mistake again.

Only *corrections* are learnt, never rewrites. If you dictate a sentence and
then rewrite it into a different sentence, that says nothing about what was
misheard, and treating it as a correction would poison the dictionary. The
similarity gate below is what separates the two.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger(__name__)

_WORD = re.compile(r"\w+(?:'\w+)?|[^\w\s]", re.UNICODE)

# A correction leaves most of the words alone; a rewrite does not. This is
# expressed as a fraction of tokens replaced rather than a similarity ratio,
# because a ratio is meaningless on short text: fixing one word of "call Jon"
# is a 50% change, and would be thrown away by any sensible ratio threshold
# even though it is the single most typical correction there is.
REPLACED_FRACTION_CEILING = 0.6
# Substitutions longer than this are almost always the user rephrasing.
MAX_PHRASE_WORDS = 3


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text or "")


def extract_corrections(original: str, corrected: str) -> list[tuple[str, str]]:
    """Word-level substitutions turning `original` into `corrected`.

    Returns (heard, meant) pairs. Empty when the edit looks like a rewrite
    rather than a correction.
    """
    original = (original or "").strip()
    corrected = (corrected or "").strip()
    if not original or not corrected or original == corrected:
        return []

    a, b = tokenize(original), tokenize(corrected)
    if not a or not b:
        return []

    matcher = SequenceMatcher(a=a, b=b, autojunk=False)
    opcodes = matcher.get_opcodes()

    kept = sum(i2 - i1 for tag, i1, i2, _, _ in opcodes if tag == "equal")
    replaced = sum(i2 - i1 for tag, i1, i2, _, _ in opcodes if tag == "replace")
    # Nothing at all preserved means this was not a fix to what was heard.
    # A one-word utterance is exempt: there is no context it could preserve,
    # and changing it is unambiguously a correction of that word.
    if len(a) > 1 and kept == 0:
        log.debug("ignoring edit: nothing preserved, looks like a rewrite")
        return []
    if len(a) > 1 and (replaced / len(a)) > REPLACED_FRACTION_CEILING:
        log.debug("ignoring edit: %.0f%% of words changed", 100 * replaced / len(a))
        return []

    pairs: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "replace":
            # Pure insertions and deletions are usually the user adding or
            # cutting words, not fixing a mishearing.
            continue
        heard = " ".join(a[i1:i2])
        meant = " ".join(b[j1:j2])
        if not heard or not meant:
            continue
        if (i2 - i1) > MAX_PHRASE_WORDS or (j2 - j1) > MAX_PHRASE_WORDS:
            continue
        if heard.lower() == meant.lower() and heard == meant:
            continue
        # Punctuation-only churn teaches nothing.
        if not any(ch.isalnum() for ch in heard) and not any(
            ch.isalnum() for ch in meant
        ):
            continue
        pairs.append((heard, meant))
    return pairs


class CorrectionLearner:
    """Counts corrections and, once one repeats, applies it automatically.

    A single correction is not acted on: you might have simply changed your
    mind. The same correction twice is a pattern worth trusting, which is what
    `min_count` encodes.
    """

    def __init__(
        self,
        path: Path | None = None,
        min_count: int = 2,
        max_entries: int = 1000,
    ) -> None:
        self.path = path
        self.min_count = min_count
        self.max_entries = max_entries
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self.load()

    # --- persistence --------------------------------------------------------

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self._counts = {
                k: int(v) for k, v in (raw.get("corrections") or {}).items()
            }
            log.info("loaded %d learned correction(s)", len(self._counts))
        except Exception:
            log.warning("could not read %s; starting fresh", self.path, exc_info=True)
            self._counts = {}

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "corrections": self._counts}
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            tmp.replace(self.path)
        except Exception:
            log.warning("could not save learned corrections", exc_info=True)

    # --- learning -----------------------------------------------------------

    @staticmethod
    def _key(heard: str, meant: str) -> str:
        return f"{heard.lower()}\x1f{meant}"

    @staticmethod
    def _split(key: str) -> tuple[str, str]:
        heard, _, meant = key.partition("\x1f")
        return heard, meant

    def observe(self, original: str, corrected: str) -> list[tuple[str, str]]:
        """Record what an edit taught us. Returns the pairs that were learnt."""
        pairs = extract_corrections(original, corrected)
        if not pairs:
            return []
        with self._lock:
            for heard, meant in pairs:
                key = self._key(heard, meant)
                self._counts[key] = self._counts.get(key, 0) + 1
                log.info(
                    "learned: %r -> %r (seen %dx)", heard, meant, self._counts[key]
                )
            if len(self._counts) > self.max_entries:
                # Drop the least-confirmed entries first.
                kept = sorted(self._counts.items(), key=lambda kv: -kv[1])
                self._counts = dict(kept[: self.max_entries])
        self.save()
        return pairs

    def record_pair(self, heard: str, meant: str, count: int = 1) -> None:
        """Add a correction directly, e.g. from the manual 'fix that' dialog.

        Manual corrections are trusted immediately - the user typed them on
        purpose - so they land at `min_count` rather than 1.
        """
        with self._lock:
            key = self._key(heard, meant)
            self._counts[key] = max(
                self._counts.get(key, 0) + count, self.min_count
            )
        self.save()

    # --- applying -----------------------------------------------------------

    def replacements(self) -> dict[str, str]:
        """Confirmed corrections, in the shape `clean_text` expects."""
        with self._lock:
            items = list(self._counts.items())
        out: dict[str, str] = {}
        best: dict[str, int] = {}
        for key, count in items:
            if count < self.min_count:
                continue
            heard, meant = self._split(key)
            # One mishearing can have competing fixes; the most-confirmed wins.
            if count > best.get(heard, 0):
                best[heard] = count
                out[heard] = meant
        return out

    def vocabulary(self, limit: int = 40) -> list[str]:
        """The words you keep correcting *to* - your jargon, in effect."""
        with self._lock:
            items = sorted(self._counts.items(), key=lambda kv: -kv[1])
        seen: list[str] = []
        for key, count in items:
            if count < self.min_count:
                continue
            _, meant = self._split(key)
            for word in meant.split():
                if any(ch.isalpha() for ch in word) and word not in seen:
                    seen.append(word)
            if len(seen) >= limit:
                break
        return seen[:limit]

    def prompt_bias(self, base: str = "", limit: int = 40) -> str:
        """Fold learnt vocabulary into whisper's `initial_prompt`.

        Biasing the decoder is strictly better than substituting after the
        fact: it can prevent the mistake instead of patching it up.
        """
        words = self.vocabulary(limit)
        if not words:
            return base
        joined = ", ".join(words)
        return f"{base.strip()} {joined}".strip() if base.strip() else joined

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            total = len(self._counts)
            confirmed = sum(1 for v in self._counts.values() if v >= self.min_count)
        return {"tracked": total, "active": confirmed}

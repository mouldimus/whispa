"""Turning a wall of transcript into something shaped like writing.

Whisper hands back one long line. That is fine for a sentence dropped into a
search box and useless for anything longer: dictate three paragraphs and you
get three paragraphs' worth of words in a single lane.

Three sources of structure, in the order they are trusted:

1. **Pauses.** People pause between paragraphs. Whisper's segments carry
   timestamps, so a gap longer than `pause_seconds` between one segment ending
   and the next starting becomes a paragraph break. This is the one that works
   without the user learning anything.
2. **Spoken commands.** "new paragraph", "bullet point", "new line" - said out
   loud, matched only where they cannot plausibly be prose (see
   `_boundary_pattern`), and extensible from the config file.
3. **Tidying.** Capitalise the first letter of each sentence and of each new
   block, collapse runs of blank lines, strip the space that ends up in front
   of punctuation when a command is removed mid-sentence.

Everything here is pure string work so it can be tested without a model, a
microphone or a keyboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Paragraph and line breaks are carried through the pipeline as private-use
# characters rather than real newlines. clean_text() and the replacement rules
# run in between, and both are much easier to reason about on a single line.
PARA_MARK = "\ue000"
LINE_MARK = "\ue001"
BULLET_MARK = "\ue002"
NUMBER_MARK = "\ue003"

_MARKS = (PARA_MARK, LINE_MARK, BULLET_MARK, NUMBER_MARK)


@dataclass(frozen=True)
class Segment:
    """One decoded chunk of audio, with its position in the original timeline."""

    text: str
    start: float = 0.0
    end: float = 0.0


# Said out loud, these mean structure rather than words. Only phrases that are
# improbable as prose are on by default - "period" and "colon" are not, because
# "the Cretaceous period" and "the colon" are things people dictate, and a
# false positive here silently mangles the sentence. The user can add those in
# `voice_commands` in config.json if they want them.
DEFAULT_COMMANDS: dict[str, str] = {
    "new paragraph": PARA_MARK,
    "next paragraph": PARA_MARK,
    "new line": LINE_MARK,
    "next line": LINE_MARK,
    "bullet point": BULLET_MARK,
    "new bullet": BULLET_MARK,
    "next bullet": BULLET_MARK,
    "next point": BULLET_MARK,
    "numbered point": NUMBER_MARK,
    "next number": NUMBER_MARK,
}

_MARK_BY_NAME = {
    "paragraph": PARA_MARK,
    "line": LINE_MARK,
    "bullet": BULLET_MARK,
    "number": NUMBER_MARK,
    "newline": LINE_MARK,
    "\n\n": PARA_MARK,
    "\n": LINE_MARK,
}


def _resolve(action: str) -> str:
    """Config values are written as 'paragraph'/'bullet'/... or literal text."""
    return _MARK_BY_NAME.get(action.strip().lower(), action)


def _boundary_pattern(phrase: str) -> re.Pattern[str]:
    """Match `phrase` only where it is an instruction, not a noun.

    A command is spoken as its own breath, so whisper nearly always writes it
    as its own sentence or clause: at the very start of the transcript, or
    after `.`, `,`, `;`, `:`, `!`, `?`. Requiring that boundary is what keeps
    "the new line of business" from turning into a line break, at the cost of
    missing a command run together with the words before it - the safe way
    round, because the failure is a visible "new paragraph" in the text rather
    than a silently chopped sentence.
    """
    words = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.compile(
        r"(?:^|(?<=[.,;:!?])|(?<=[\ue000\ue001\ue002\ue003]))"
        r"\s*" + words + r"\s*[.,;:!?]?",
        re.IGNORECASE,
    )


def _punctuation_pattern(phrase: str) -> re.Pattern[str]:
    """Match a spoken punctuation command anywhere, with the debris around it.

    Whisper punctuates what it hears, so "full stop" arrives already wrapped in
    commas or a stop of its own ("three, full stop, four"). Swallow that, and
    the punctuation the user actually asked for lands in its place. There is no
    boundary check here because these commands only exist if the user added
    them to `voice_command_extras` themselves - they have accepted that saying
    "period" now means one.
    """
    words = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.compile(
        r"[ \t]*[.,;:!?]?[ \t]*\b" + words + r"\b[ \t]*[.,;:!?]?[ \t]*",
        re.IGNORECASE,
    )


def apply_voice_commands(text: str, commands: dict[str, str] | None = None) -> str:
    """Replace spoken structure commands with their marks."""
    if not text:
        return ""
    table = DEFAULT_COMMANDS if commands is None else commands
    if not table:
        return text
    # Longest phrase first: "next paragraph" must win over any future "next".
    for phrase in sorted(table, key=len, reverse=True):
        mark = _resolve(table[phrase])
        if mark and mark not in _MARKS and mark[0] in ".,;:!?":
            text = _punctuation_pattern(phrase).sub(mark + " ", text)
        else:
            text = _boundary_pattern(phrase).sub(mark, text)
    return text


_FILLER_RE = re.compile(r"(?i)(?:,\s*)?\b(?:um+|uh+|erm+|hmm+|mm+)\b(?:\s*,)?\s*")

# An immediately repeated word or short phrase is a stutter, not emphasis -
# "the the meeting", "no no no". Collapsed to the first occurrence; applied
# repeatedly (see remove_disfluencies) so a run of any length collapses fully.
_REPEAT_RE = re.compile(r"(?i)\b([A-Za-z']+(?:\s+[A-Za-z']+){0,2})\b([ \t]*,?[ \t]+)\1\b")

# Said out loud, these mean "ignore what I just said" - not "here is more
# content". Kept short and unambiguous on purpose: a false positive here
# silently deletes real words, which is worse than leaving a false start in
# the transcript for the tray's "Fix last dictation..." to clean up.
CORRECTION_CUES: tuple[str, ...] = (
    "scratch that",
    "strike that",
    "disregard that",
)


def _correction_pattern(cue: str) -> re.Pattern[str]:
    words = r"[,\s]+".join(re.escape(w) for w in cue.split())
    boundary = r"[.!?]"
    return re.compile(
        rf"(?:(?<={boundary})\s*|^)[^.!?]*?"
        rf",?\s*\b{words}\b[,]?\s*",
        re.IGNORECASE,
    )


_CORRECTION_PATTERNS = [(cue, _correction_pattern(cue)) for cue in
                        sorted(CORRECTION_CUES, key=len, reverse=True)]


def remove_disfluencies(text: str) -> str:
    """Clean up the mess real, unscripted speech leaves in a transcript.

    Three passes, cheapest and safest first:

    1. Filler words ("um", "uh", "erm") - dropped outright, wherever they are.
    2. An immediately repeated word or short phrase - a stutter, collapsed to
       one occurrence.
    3. An explicit spoken correction ("scratch that", "strike that", ...) -
       the false start since the last sentence boundary is dropped along with
       the cue phrase itself, keeping only what follows.

    Pass 3 is the only one that deletes content beyond the disfluency itself,
    which is why its cue list is short and unambiguous - see CORRECTION_CUES.
    It only reaches back to the nearest boundary: "meet at three, scratch
    that, meet at four" is handled, but if the false start got a full stop of
    its own ("Meet at three. Scratch that. Meet at four.") only the cue
    phrase goes, since reaching back a whole extra sentence risks eating
    something unrelated. Ordinary speech rarely pauses long enough before a
    correction to earn that period, so this is the uncommon case.
    """
    if not text:
        return ""
    text = _FILLER_RE.sub(" ", text)
    for _ in range(3):
        collapsed = _REPEAT_RE.sub(r"\1", text)
        if collapsed == text:
            break
        text = collapsed
    for _cue, pattern in _CORRECTION_PATTERNS:
        text = pattern.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _ends_with_punctuation(text: str) -> bool:
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] in ".,;:!?"


def join_segments(
    segments: list[Segment] | list,
    pause_seconds: float = 1.0,
    comma_seconds: float = 0.0,
) -> str:
    """Join decoded segments, marking a paragraph break at every long pause.

    A shorter pause than that - real, but not paragraph-length - gets a comma
    instead, if the segment before it doesn't already end with one. Measured
    against real audio, whisper reliably drops the comma it would otherwise
    write once a gap gets past about a second, and a plain word-join at that
    point runs two clauses together with nothing to mark the pause at all.
    """
    parts: list[str] = []
    previous_end: float | None = None
    for seg in segments:
        piece = (getattr(seg, "text", "") or "").strip()
        if not piece:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        if parts:
            gap = start - previous_end if previous_end is not None else 0.0
            # A negative gap means the timestamps overlap, which happens around
            # VAD boundaries; treat it as no pause rather than a break.
            if pause_seconds > 0 and gap >= pause_seconds:
                sep = PARA_MARK
            elif comma_seconds > 0 and gap >= comma_seconds and not _ends_with_punctuation(parts[-1]):
                sep = ", "
            else:
                sep = " "
            parts.append(sep)
        parts.append(piece)
        previous_end = float(getattr(seg, "end", start) or start)
    return "".join(parts)


def _capitalise_sentences(text: str) -> str:
    def upper_first(match: re.Match[str]) -> str:
        return match.group(0).upper()

    # Start of a block, after a list prefix, and after a sentence ends.
    text = re.sub(r"(?m)^[ \t]*[a-z]", upper_first, text)
    text = re.sub(
        r"(?m)^([-*\u2022][ \t]+|\d+\.[ \t]+)([a-z])",
        lambda m: m.group(1) + m.group(2).upper(),
        text,
    )
    text = re.sub(r"(?<=[.!?])(\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text


def _renumber(text: str) -> str:
    """Give the numbered-list marks their running numbers, per block."""
    out: list[str] = []
    counter = 0
    for line in text.split("\n"):
        if line.startswith(NUMBER_MARK):
            counter += 1
            out.append(f"{counter}. " + line[len(NUMBER_MARK):].lstrip())
        else:
            if line.strip():
                # Any non-list line ends the run, so a second list later in the
                # dictation starts again at 1.
                counter = 0
            out.append(line)
    return "\n".join(out)


def render_marks(
    text: str,
    paragraph_style: str = "blank",
    bullet: str = "- ",
) -> str:
    """Turn the private-use marks into real whitespace and list prefixes."""
    if not text:
        return ""
    if paragraph_style == "off":
        for mark in _MARKS:
            text = text.replace(mark, " ")
        return re.sub(r"[ \t]+", " ", text).strip()

    para = "\n" if paragraph_style == "single" else "\n\n"
    text = text.replace(PARA_MARK, para)
    text = text.replace(LINE_MARK, "\n")
    text = text.replace(BULLET_MARK, "\n" + BULLET_MARK)
    text = text.replace(NUMBER_MARK, "\n" + NUMBER_MARK)
    text = _renumber(text)
    text = text.replace(BULLET_MARK, bullet)
    # Space that used to separate the words either side of a removed command.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # A break inserted right before punctuation is the command being spoken a
    # beat late; keep the punctuation with the sentence it belongs to.
    text = re.sub(r"\n+([.,;:!?])", r"\1", text)
    return text.strip()


def format_text(
    text: str,
    paragraph_style: str = "blank",
    voice_commands: dict[str, str] | None = None,
    use_voice_commands: bool = True,
    capitalise: bool = True,
    bullet: str = "- ",
    strip_disfluencies: bool = False,
) -> str:
    """Full text-shaping pass: disfluencies -> commands -> marks -> real formatting."""
    if not text:
        return ""
    if strip_disfluencies:
        text = remove_disfluencies(text)
    if use_voice_commands:
        text = apply_voice_commands(text, voice_commands)
    text = render_marks(text, paragraph_style=paragraph_style, bullet=bullet)
    if capitalise:
        text = _capitalise_sentences(text)
    return text


def strip_marks(text: str) -> str:
    """Remove any marks that survived, so they can never reach the clipboard."""
    for mark in _MARKS:
        text = text.replace(mark, " ")
    return text


def command_table(extras: dict[str, str] | None = None) -> dict[str, str]:
    """The default commands, plus (or minus) whatever the config adds.

    An empty value removes a default, which is how someone who says "next
    point" in ordinary conversation turns just that one off without losing the
    rest.
    """
    table = {phrase: mark for phrase, mark in DEFAULT_COMMANDS.items()}
    for phrase, action in (extras or {}).items():
        key = (phrase or "").strip().lower()
        if not key:
            continue
        if not action:
            table.pop(key, None)
        else:
            table[key] = _resolve(action)
    return table


def make_formatter(cfg):
    """Build the callable the engine applies to every finished transcript."""
    commands = command_table(getattr(cfg, "voice_command_extras", None))
    use_commands = bool(getattr(cfg, "voice_commands", True))
    style = getattr(cfg, "paragraph_style", "blank")
    capitalise = bool(getattr(cfg, "auto_capitalise", True))
    bullet = getattr(cfg, "bullet_prefix", "- ") or "- "
    strip_disfluencies = bool(getattr(cfg, "remove_disfluencies", True))

    def _format(text: str) -> str:
        return format_text(
            text,
            paragraph_style=style,
            voice_commands=commands,
            use_voice_commands=use_commands,
            capitalise=capitalise,
            bullet=bullet,
            strip_disfluencies=strip_disfluencies,
        )

    return _format

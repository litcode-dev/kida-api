"""Keep abusive text out of the fields a person reads by hand.

The loop request queue is worked by a human: an admin opens the listing and
reads every artist name, song title and note that was submitted. Nothing else
filters that text, so this does.

Matching is deliberately narrow, because a false positive here rejects a real
request:

* Terms match as **whole words**, so "class", "Scunthorpe", "cocktail" and
  "assess" all pass. This is the check that matters most — substring matching
  is what makes naive filters unusable.
* Common suffixes are allowed, so one entry covers "fucking" and "bitches".
* Masking characters *inside* a word are seen through — "f*ck", "f.u.c.k",
  "sh!t" — and so are digit substitutions ("sh1t") and stretched letters
  ("fuuuck"). Whitespace is not: catching "f u c k" would reject far more
  innocent text than the evasion is worth.

There are two checks, because the fields are not alike. ``find_banned_term``
applies both lists and guards the submitter's own words — a note, a link.
``find_slur`` applies the hateful list only and guards the two fields that
quote somebody else's record: plenty of real releases are called things nobody
would put in a note, and refusing them rejects the request rather than the
behaviour.

Both lists below are a starting point, not a policy. They are plain tuples so
that adding a term someone actually used is a one-line change.
"""
import re
import unicodedata

# General profanity. Coarse rather than hateful — the register nobody wants in
# a support inbox.
PROFANITY = (
    "arsehole", "asshole", "bastard", "bitch", "bollocks", "bullshit",
    "cock", "cunt", "dick", "dickhead", "dumbass", "fuck", "jackass",
    "jerkoff", "motherfucker", "prick", "pussy", "shit", "slut", "twat",
    "wanker", "whore",
)

# Slurs. Kept apart from the list above because they are a different kind of
# problem: profanity is rude, these target a person for what they are.
SLURS = (
    "chink", "coon", "dyke", "faggot", "gook", "kike", "nigga", "nigger",
    "paki", "raghead", "retard", "spic", "towelhead", "tranny", "wetback",
)

BANNED_TERMS = PROFANITY + SLURS

# Digit and symbol stand-ins, per letter. Only the substitutions that read
# unambiguously as that letter — "l" for "i" is left out, as it turns ordinary
# words into matches.
_LEET = {
    "a": "a4@",
    "b": "b8",
    "e": "e3",
    "g": "g9",
    "i": "i1!",
    "l": "l|",
    "o": "o0",
    "s": "s5$",
    "t": "t7",
}

# Suffixes one entry should cover: fucking, bitches, wankers, bitchin.
_SUFFIX = r"(?:s|es|ed|ing|in|er|ers|y|z|a)?"

# Masking characters between letters — punctuation and underscores, never
# whitespace. Bounded so a match cannot run across half a sentence.
_GAP = r"[^\w\s]{0,3}"


def _letter(char: str) -> str:
    """One letter: itself, a stand-in digit, or a single masking symbol.

    The symbol branch is what catches "f*ck" — a mask replacing the letter
    rather than sitting beside it. On its own that branch would let a run of
    punctuation satisfy a whole term, so ``find_banned_term`` also requires a
    match to carry real letters.
    """
    variants = _LEET.get(char, char)
    letter = f"[{re.escape(variants)}]+" if len(variants) > 1 else f"{re.escape(char)}+"
    return f"(?:{letter}|[^\\w\\s])"


def _pattern(term: str) -> str:
    body = _GAP.join(_letter(c) for c in term)
    # Lookarounds rather than \b: the term may end in a character class, and a
    # trailing letter or digit must block the match either way.
    return f"(?<![a-z0-9]){body}{_SUFFIX}(?![a-z0-9])"


def _compile(terms) -> re.Pattern:
    return re.compile("|".join(_pattern(t) for t in terms), re.IGNORECASE)


_MATCHER = _compile(BANNED_TERMS)
_SLUR_MATCHER = _compile(SLURS)


# Four or more single letters in a row, each separated by whitespace — someone
# spelling a word out to walk past the filter. Four is the floor because real
# text does produce short runs ("R & B", "U S A") but almost never longer ones.
_SPELLED_OUT = re.compile(r"(?<![a-z0-9])(?:[a-z]\s+){3,}[a-z](?![a-z0-9])")


def _collapse_spelled_out(text: str) -> str:
    """Close the gaps in "b i t c h", leaving everything else alone."""
    return _SPELLED_OUT.sub(lambda m: re.sub(r"\s+", "", m.group(0)), text)


def _normalise(text: str) -> str:
    """Fold case and strip accents, so "fück" is read as "fuck".

    NFKD splits an accented character into its base letter plus a combining
    mark; dropping the marks leaves the letter. It also unpacks lookalike
    forms — full-width and circled letters — into plain ASCII.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return without_marks.casefold()


# A match has to be mostly letters. Without this, the masking branch in
# ``_letter`` lets "****" stand in for a four-letter term, and someone
# redacting their own text gets rejected for it. Every term is at least four
# letters, so three real characters allows one masked letter and no more.
_MIN_REAL_CHARS = 3


def _search(matcher: re.Pattern, text: str | None) -> str | None:
    if not text:
        return None
    normalised = _normalise(text)
    # Both forms, because collapsing only ever adds detections: the plain text
    # catches everything ordinary, the collapsed one catches the spelled-out
    # evasion without letting it rewrite the rest of the string.
    for candidate in (normalised, _collapse_spelled_out(normalised)):
        for match in matcher.finditer(candidate):
            fragment = match.group(0)
            if sum(c.isalnum() for c in fragment) >= _MIN_REAL_CHARS:
                return fragment
    return None


def find_banned_term(text: str | None) -> str | None:
    """Profanity or a slur. Return the offending fragment, or None if clean.

    The fragment is returned as it appeared after normalisation, which is what
    makes a rejection debuggable — the caller decides whether to repeat it back.
    """
    return _search(_MATCHER, text)


def find_slur(text: str | None) -> str | None:
    """Slurs only — the check for a field that quotes someone else's work.

    Real records are called things no one would write in a support note. A
    title or an artist name has to be reproduced as it is, or the request
    cannot be fulfilled, so profanity there is the work rather than an insult
    and only the hateful list applies.
    """
    return _search(_SLUR_MATCHER, text)


def is_clean(text: str | None) -> bool:
    return find_banned_term(text) is None

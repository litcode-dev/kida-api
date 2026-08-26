"""The moderation matcher, judged on the two ways a filter fails.

A filter that rejects real submissions is worse than no filter, so the
false-positive set below is the more important half of this file.
"""
import pytest

from app.utils.text_moderation import (
    BANNED_TERMS,
    PROFANITY,
    SLURS,
    find_banned_term,
    find_slur,
    is_clean,
)


# Words and phrases that contain a banned term as a substring, or otherwise
# look suspicious to a naive filter. Every one must pass.
CLEAN = [
    "Love Me JeJe",
    "Class of 2026",
    "Scunthorpe Sessions",
    "cocktail hour",
    "assess the mix",
    "Peacock",
    "Dickens",
    "Essex",
    "Sussex",
    "shiitake",
    "Analysis",
    "Bassline",
    "Massive",
    "Cockburn",
    "Hitchcock",
    "classic",
    "glasses",
    "Please make it mellow, around 92 BPM",
    "https://open.spotify.com/track/4cOdK2wGLETKBW3PKybEgs",
    "R & B",
    "U S A",
    "a.s.a.p",
    "Afrobeat @ 105bpm",
    "Feat. Wizkid & Tiwa",
    "100% dry mix",
    "Track ****",
    "***",
]

DIRTY = [
    "fuck this",
    "What the f*ck",
    "sh1t mix",
    "f.u.c.k",
    "FUUUCK",
    "bitches",
    "motherfucker",
    "you are a retard",
    "stupid b!tch",
    "wankers",
    "fück",
    "cunt",
    "c*nt",
    "B I T C H",
    "f u c k off",
]


@pytest.mark.parametrize("text", CLEAN)
def test_ordinary_text_passes(text):
    assert find_banned_term(text) is None, f"false positive on {text!r}"


@pytest.mark.parametrize("text", DIRTY)
def test_abuse_is_caught(text):
    assert find_banned_term(text) is not None, f"missed {text!r}"


@pytest.mark.parametrize("term", BANNED_TERMS)
def test_every_listed_term_matches_itself(term):
    """A term that its own matcher misses is a typo in the list."""
    assert find_banned_term(f"a song about {term} really") is not None


def test_the_offending_fragment_is_returned():
    assert find_banned_term("what the f*ck is this") == "f*ck"


def test_a_term_inside_a_longer_word_is_not_a_match():
    """The check that makes the difference between usable and unusable."""
    assert is_clean("classic bassline")
    assert is_clean("Cockburn and Dickens")


def test_suffixes_are_covered_by_one_entry():
    for variant in ("fucking", "fucked", "fucker", "fuckers"):
        assert not is_clean(variant)


def test_punctuation_alone_is_not_a_match():
    """Masking every letter would otherwise turn redaction into an offence."""
    assert is_clean("****")
    assert is_clean("* * * *")


def test_empty_and_missing_text_is_clean():
    assert is_clean(None)
    assert is_clean("")


# --- the narrower check, for fields that quote somebody else's record --------

# Real releases. A filter that refuses these refuses the request, not the
# behaviour — the submitter cannot spell the title any other way.
REAL_TITLES = [
    "Fuck tha Police",
    "Bitch Better Have My Money",
    "Dick Dale",
    "The Shit",
    "Cocky",
]


@pytest.mark.parametrize("title", REAL_TITLES)
def test_profanity_in_a_title_passes_the_slur_check(title):
    assert find_slur(title) is None


@pytest.mark.parametrize("title", REAL_TITLES)
def test_the_same_titles_would_have_been_refused_by_the_full_check(title):
    """Documents why the two checks exist rather than one."""
    assert find_banned_term(title) is not None


@pytest.mark.parametrize("term", SLURS)
def test_every_slur_is_caught_by_the_narrow_check(term):
    assert find_slur(f"a song about {term} really") is not None


@pytest.mark.parametrize("term", PROFANITY)
def test_no_profanity_is_caught_by_the_narrow_check(term):
    assert find_slur(f"a song about {term} really") is None


def test_the_narrow_check_still_sees_through_obfuscation():
    assert find_slur("n1gger") is not None
    assert find_slur("you r*tard") is not None

"""Property-based tests (Hypothesis) for the deterministic grounding code.

Grounding is the system's main guarantee, so its two halves are tested against
generated inputs rather than a handful of examples:

- quote matching tolerates scattered OCR-style misreads, but never accepts a
  quote with text appended that is not in the source;
- date parsing round-trips every supported format and never raises.

``derandomize=True`` makes runs reproducible in CI.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from medchron.grounding.dates import find_dates, parse_date
from medchron.grounding.quotes import FUZZY_THRESHOLD, find_quote, normalize

SETTINGS = settings(max_examples=150, deadline=None, derandomize=True)

# Source text uses letters a-m only. Fabricated additions use n-z, so they can
# never be found in the source by accident.
SOURCE_LETTERS = "abcdefghijklm"
FABRICATED_LETTERS = "nopqrstuvwxyz"
source_words = st.text(alphabet=SOURCE_LETTERS, min_size=3, max_size=9)
fabricated_words = st.text(alphabet=FABRICATED_LETTERS, min_size=3, max_size=9)

# Single-character OCR confusions that keep the quote's length.
CONFUSIONS = {"l": "1", "i": "l", "e": "c", "b": "h", "g": "q", "a": "o", "m": "n"}


@st.composite
def page_and_quote(draw: st.DrawFn) -> tuple[str, str, int]:
    """A page of text, a quote taken from it, and where the quote starts."""
    words = draw(st.lists(source_words, min_size=25, max_size=80, unique=True))
    seps = draw(st.lists(st.sampled_from([" ", " ", "  ", "\n"]), min_size=len(words),
                         max_size=len(words)))  # fmt: skip
    text = "".join(w + s for w, s in zip(words, seps, strict=True))
    first = draw(st.integers(0, len(words) - 8))
    count = draw(st.integers(6, min(16, len(words) - first)))
    quote = " ".join(words[first : first + count])
    start = text.index(words[first] + seps[first])  # words are unique
    return text, quote, start


def _noisy(quote: str, positions: list[int]) -> str:
    chars = list(quote)
    for p in positions:
        if chars[p] in CONFUSIONS:
            chars[p] = CONFUSIONS[chars[p]]
    return "".join(chars)


@SETTINGS
@given(page_and_quote(), st.sampled_from(["", "  ", "\n"]))
def test_exact_quotes_are_found_at_their_offsets(case: tuple[str, str, int], pad: str) -> None:
    text, quote, start = case
    m = find_quote(text, pad + quote.upper() + pad)  # case and whitespace do not matter
    assert m is not None and m.exact
    assert m.start == start
    assert normalize(text[m.start : m.end]) == normalize(quote)


@SETTINGS
@given(page_and_quote(), st.data())
def test_scattered_ocr_misreads_are_tolerated(
    case: tuple[str, str, int], data: st.DataObject
) -> None:
    text, quote, start = case
    # At most one misread per 12 characters, at least 6 characters apart.
    budget = len(quote) // 12
    positions = data.draw(
        st.lists(st.integers(0, len(quote) - 1), max_size=budget, unique=True).filter(
            lambda ps: all(abs(a - b) >= 6 for a in ps for b in ps if a != b)
        )
    )
    noisy = _noisy(quote, positions)
    m = find_quote(text, noisy)
    assert m is not None, (quote, noisy)
    assert m.score >= FUZZY_THRESHOLD
    true_end = start + len(normalize(quote))  # source separators may be wider; overlap check
    overlap = min(m.end, true_end) - max(m.start, start)
    assert overlap >= 0.8 * len(normalize(quote))


@SETTINGS
@given(page_and_quote(), st.lists(fabricated_words, min_size=1, max_size=4))
def test_fabricated_additions_are_rejected(case: tuple[str, str, int], extra: list[str]) -> None:
    text, quote, _ = case
    addition = " ".join(extra)
    assume(len(addition) > 4)  # longer than a plausible OCR slip
    assert find_quote(text, quote + " " + addition) is None
    assert find_quote(text, addition + " " + quote) is None
    words = quote.split()
    middle = " ".join([*words[:3], addition, *words[3:]])
    assert find_quote(text, middle) is None


@SETTINGS
@given(page_and_quote(), st.lists(fabricated_words, min_size=2, max_size=8))
def test_wholly_fabricated_quotes_are_rejected(
    case: tuple[str, str, int], words: list[str]
) -> None:
    text, _, _ = case
    assert find_quote(text, " ".join(words)) is None


FORMATS = ["%m/%d/%Y", "%B %d, %Y", "%d-%b-%Y", "%Y-%m-%d", "%b. %d, %Y", "%b %d %Y"]


@SETTINGS
@given(st.dates(dt.date(1900, 1, 1), dt.date(2099, 12, 31)), st.sampled_from(FORMATS))
def test_dates_round_trip_in_every_format(day: dt.date, fmt: str) -> None:
    written = day.strftime(fmt)
    assert parse_date(f"Seen on {written} in clinic.") == day
    found = find_dates(f"Admitted {written}; discharged later.")
    assert found and found[0][0] == day and found[0][1] in f"Admitted {written}"


@SETTINGS
@given(st.dates(dt.date(1970, 1, 1), dt.date(2069, 12, 31)))
def test_two_digit_years_use_a_fixed_pivot(day: dt.date) -> None:
    assert parse_date(day.strftime("%m-%d-%y")) == day


@SETTINGS
@given(st.text(max_size=200))
def test_date_parsing_never_raises(text: str) -> None:
    result = parse_date(text)
    assert result is None or isinstance(result, dt.date)
    for day, written in find_dates(text):
        assert written in text and isinstance(day, dt.date)


IMPOSSIBLE = [(2, 30), (2, 31), (4, 31), (6, 31), (9, 31), (11, 31)]


@SETTINGS
@given(st.sampled_from(IMPOSSIBLE), st.integers(1901, 2099), st.sampled_from(["/", "-"]))
def test_impossible_calendar_dates_are_not_invented(
    month_day: tuple[int, int], year: int, sep: str
) -> None:
    month, day = month_day
    written = f"{month:02d}/{day:02d}/{year}" if sep == "/" else f"{year}-{month:02d}-{day:02d}"
    assert parse_date(written) is None


@SETTINGS
@given(st.integers(1850, 2150))
def test_february_29_only_in_leap_years(year: int) -> None:
    expected = dt.date(year, 2, 29) if calendar.isleap(year) else None
    assert parse_date(f"02/29/{year}") == expected


def test_quote_offsets_survive_irregular_whitespace() -> None:
    text = "Labs:\n\n  WBC   16.4  x10^3/uL (H).\nLipase   normal."
    m = find_quote(text, "wbc 16.4 x10^3/ul (h). lipase normal.")
    assert m is not None and re.sub(r"\s+", " ", text[m.start : m.end]).lower().startswith("wbc")

"""Detect pages that appear to belong to a different patient (misfiled pages).

Record productions sometimes contain another patient's page. Producing it
onward is a privacy problem, and extracting from it corrupts the chronology.
Most record pages carry a patient header (name and date of birth). This module
finds the identity shared by most pages and flags pages whose header names a
clearly different person. The rule is deterministic and conservative: OCR
noise in a name is tolerated, and pages without a readable header are never
flagged. A flagged page is excluded from extraction and goes to a reviewer.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from medchron.grounding.dates import parse_date
from medchron.ingestion.models import Page

_HEADER = re.compile(
    r"\b(?:patient(?:\s+name)?|pt\.?\s*name|name)\s*:\s*"
    r"(?P<name>[A-Za-z][A-Za-z'.\-]*(?:\s[A-Za-z][A-Za-z'.\-]*){1,3}?)\s+"
    r"(?:dob|d\.o\.b\.?|date of birth)\s*:\s*(?P<dob>.+?)(?=\s{2,}|\s+mrn\b|$)",
    re.IGNORECASE | re.MULTILINE,
)
NAME_SIMILARITY_MIN = 0.75  # below this, the names are different people
NAME_SIMILARITY_WITH_DOB = 0.9  # when birth dates differ too, a looser name match is enough


@dataclass(frozen=True)
class PatientHeader:
    name: str
    dob: dt.date | None


def page_identity(text: str) -> PatientHeader | None:
    m = _HEADER.search(text)
    if not m:
        return None
    name = re.sub(r"\s+", " ", m["name"]).strip().lower()
    return PatientHeader(name=name, dob=parse_date(m["dob"]))


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def find_misfiled(pages: list[Page]) -> list[int]:
    """Page numbers whose patient header names someone other than the majority patient."""
    ids = {p.page_no: page_identity(p.text) for p in pages if p.duplicate_of is None}
    found = {n: h for n, h in ids.items() if h is not None}
    if len(found) < 3:  # too few headers to know who the record is about
        return []
    majority = Counter(h.name for h in found.values()).most_common(1)[0][0]
    dobs = Counter(h.dob for h in found.values() if h.name == majority and h.dob)
    majority_dob = dobs.most_common(1)[0][0] if dobs else None
    flagged = []
    for n, h in sorted(found.items()):
        sim = _similar(h.name, majority)
        dob_differs = bool(h.dob and majority_dob and h.dob != majority_dob)
        if sim < NAME_SIMILARITY_MIN or (dob_differs and sim < NAME_SIMILARITY_WITH_DOB):
            flagged.append(n)
    return flagged

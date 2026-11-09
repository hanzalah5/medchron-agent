"""Case builder: a small DSL the scenarios use to write documents.

Scenarios register clinical *events* and write *documents* made of
paragraphs. A paragraph can cite an event (it becomes a gold source once
pages are laid out) or carry an evidence *tag* that a planted issue refers to.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field

from medchron.synth import names
from medchron.synth.models import DocType, EventType, IssueKind, SyntheticPatient

DATE_FORMATS = ["%m/%d/%Y", "%B %d, %Y", "%d-%b-%Y", "%Y-%m-%d", "%m-%d-%y"]


@dataclass(frozen=True)
class Facility:
    name: str
    mrn: str
    date_format: str


@dataclass
class Para:
    text: str
    event_key: str | None = None
    quote: str | None = None
    tags: tuple[str, ...] = ()


@dataclass
class DocDraft:
    doc_id: str
    doc_type: DocType
    title: str
    date: dt.date
    facility: Facility
    provider: str
    paras: list[Para] = field(default_factory=list)


@dataclass
class EventSpec:
    key: str
    date: dt.date
    type: EventType
    summary: str
    facility: str
    provider: str


@dataclass
class IssueDraft:
    kind: IssueKind
    description: str
    event_keys: list[str]
    tags: list[str]


class CaseBuilder:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        sex = rng.choice(["F", "M"])
        self.patient = SyntheticPatient(
            name=f"{rng.choice(names.FIRST_NAMES)} {rng.choice(names.LAST_NAMES)}",
            dob=dt.date(rng.randint(1950, 1995), rng.randint(1, 12), rng.randint(1, 28)),
            sex=sex,
        )
        self.events: dict[str, EventSpec] = {}
        self.docs: list[DocDraft] = []
        self.issues: list[IssueDraft] = []
        self._used_clinicians: set[str] = set()

    # ----- people and places -------------------------------------------------
    def facility(self, pool: list[str]) -> Facility:
        name = self.rng.choice(pool)
        mrn = f"{self.rng.choice('ABCDEFGHKMNPRSTX')}{self.rng.randint(1000000, 9999999)}"
        return Facility(name=name, mrn=mrn, date_format=self.rng.choice(DATE_FORMATS))

    def clinician(self, credential: str) -> str:
        while True:
            name = (
                f"{self.rng.choice(names.CLINICIAN_FIRST_INITIAL)}. "
                f"{self.rng.choice(names.CLINICIAN_LAST)}, {credential}"
            )
            if name not in self._used_clinicians:
                self._used_clinicians.add(name)
                return name

    # ----- documents ----------------------------------------------------------
    def doc(
        self, doc_type: DocType, title: str, date: dt.date, facility: Facility, provider: str
    ) -> DocDraft:
        draft = DocDraft(
            doc_id=f"D{len(self.docs) + 1:03d}",
            doc_type=doc_type,
            title=title,
            date=date,
            facility=facility,
            provider=provider,
        )
        self.docs.append(draft)
        return draft

    def remove_doc(self, draft: DocDraft) -> None:
        """Drop a document from the production (used to plant missing records)."""
        self.docs.remove(draft)

    @staticmethod
    def fmt(facility: Facility, date: dt.date) -> str:
        return date.strftime(facility.date_format)

    def para(self, doc: DocDraft, text: str, *tags: str) -> None:
        doc.paras.append(Para(text=text, tags=tags))

    def event(
        self,
        key: str,
        doc: DocDraft,
        date: dt.date,
        type_: EventType,
        summary: str,
        quote: str,
        prefix: str = "",
        tags: tuple[str, ...] = (),
    ) -> None:
        """Register an event (first time) and cite it from ``doc`` with ``quote``."""
        if key not in self.events:
            self.events[key] = EventSpec(
                key=key,
                date=date,
                type=type_,
                summary=summary,
                facility=doc.facility.name,
                provider=doc.provider,
            )
        doc.paras.append(Para(text=prefix + quote, event_key=key, quote=quote, tags=tags))

    def cite(self, key: str, doc: DocDraft, quote: str, prefix: str = "") -> None:
        """Add another source for an already-registered event."""
        if key not in self.events:
            raise KeyError(f"unknown event {key!r}")
        doc.paras.append(Para(text=prefix + quote, event_key=key, quote=quote))

    def issue(
        self,
        kind: IssueKind,
        description: str,
        event_keys: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        self.issues.append(IssueDraft(kind, description, event_keys or [], tags or []))

    # ----- realistic padding ----------------------------------------------------
    def filler(self, doc: DocDraft, kinds: list[str]) -> None:
        for kind in kinds:
            options = FILLER[kind]
            self.para(doc, self.rng.choice(options))


FILLER: dict[str, list[str]] = {
    "ros": [
        "Review of systems: Constitutional negative for weight change. HEENT negative. "
        "Cardiovascular negative for chest pain or palpitations. Respiratory negative for "
        "cough. Genitourinary negative for dysuria. Skin negative for rash. Neurologic "
        "negative for focal weakness. All other systems reviewed and negative except as noted.",
        "ROS: 10-point review of systems performed and negative except as documented in the "
        "history of present illness. Patient denies headache, vision change, chest pain, "
        "shortness of breath at rest, urinary symptoms, or new rash.",
    ],
    "exam": [
        "Physical exam: General - alert, oriented, in no acute distress. HEENT - normocephalic, "
        "mucous membranes moist. Neck - supple. Lungs - clear to auscultation bilaterally. "
        "Heart - regular rate and rhythm, no murmur. Extremities - no cyanosis. Neuro - grossly "
        "intact. Skin - warm and dry.",
        "Exam: Vitals reviewed. Well-appearing. Cardiopulmonary exam unremarkable. Abdomen "
        "soft. Extremities without gross deformity. Mental status at baseline per patient.",
    ],
    "history": [
        "Past medical history: hyperlipidemia, seasonal allergies. Past surgical history: "
        "tonsillectomy in childhood. Family history: father with hypertension. Social history: "
        "never smoker, drinks alcohol socially, works in an office setting.",
        "PMH: mild intermittent asthma, GERD. PSH: none reported. Allergies: no known drug "
        "allergies. Social: former smoker, quit over ten years ago. Lives with family.",
    ],
    "nursing": [
        "Nursing: Patient resting in bed, call light within reach. Fall precautions in place. "
        "IV site clean, dry and intact. Tolerating diet. Ambulated in hallway with assistance. "
        "Pain reassessed after medication per protocol.",
        "Nursing note: Vital signs per flowsheet. Patient educated on incentive spirometer use "
        "and verbalized understanding. Bed in low position. Family at bedside this shift.",
    ],
    "plan_generic": [
        "Medication reconciliation completed and reviewed with the patient. Patient given "
        "opportunity to ask questions; all questions answered. Patient agrees with plan.",
        "Discussed risks, benefits and alternatives of the plan of care. Patient verbalized "
        "understanding. Printed after-visit summary provided.",
    ],
    "vitals": [
        "Vitals: BP 128/82, HR 84, RR 16, SpO2 98% on room air, Temp 36.8 C.",
        "Vitals: BP 134/86, HR 78, RR 14, SpO2 97% on room air, Temp 37.0 C.",
        "Vitals: BP 122/76, HR 90, RR 18, SpO2 98% on room air, Temp 36.9 C.",
    ],
    "attestation": [
        "I personally evaluated the patient and reviewed the available records. Documentation "
        "completed with the assistance of templated text and reviewed for accuracy.",
        "Electronically signed. This note may contain transcription errors; please contact "
        "the author with any questions regarding its content.",
    ],
}

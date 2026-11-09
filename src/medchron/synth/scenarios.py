"""Clinical scenarios for the synthetic benchmark.

Each scenario writes a realistic multi-provider record set for a fictional
patient and plants *issues* (gaps, conflicts, potential delays) with exact
gold labels. Clinical content is simplified and illustrative only; issue
descriptions are phrased as questions for expert review, never conclusions.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from medchron.synth import names
from medchron.synth.builder import CaseBuilder
from medchron.synth.models import DocType, EventType, IssueKind

E = EventType


@dataclass(frozen=True)
class ScenarioOptions:
    date_conflict: bool
    missing_record: bool


def _day(b: CaseBuilder, year_lo: int = 2022, year_hi: int = 2024) -> dt.date:
    return dt.date(b.rng.randint(year_lo, year_hi), b.rng.randint(1, 12), b.rng.randint(1, 28))


def _shift(d: dt.date, days: int) -> dt.date:
    return d + dt.timedelta(days=days)


# --------------------------------------------------------------------------- #
# Scenario 1: abdominal pain, discharged, returns with perforated appendicitis #
# --------------------------------------------------------------------------- #
def abdominal_pain_return_visit(b: CaseBuilder, o: ScenarioOptions) -> None:
    t0 = _day(b)
    hosp = b.facility(names.HOSPITALS)
    pcp = b.facility(names.CLINICS)
    pcp_md = b.clinician("MD")
    ed1_md = b.clinician("DO")
    ed2_md = b.clinician("MD")
    surgeon = b.clinician("MD, FACS")
    hospitalist = b.clinician("MD")
    radiologist = b.clinician("MD")
    wbc = round(b.rng.uniform(13.5, 17.5), 1)
    gap_days = b.rng.choice([2, 3])
    t1 = _shift(t0, gap_days)
    los = b.rng.randint(4, 6)
    t_dc = _shift(t1, los)
    f = b.fmt

    # Baseline PCP visit
    d_pre = _shift(t0, -b.rng.randint(60, 200))
    doc = b.doc(DocType.OFFICE_VISIT, "Office Visit - Annual Exam", d_pre, pcp, pcp_md)
    b.event(
        "pcp_annual",
        doc,
        d_pre,
        E.ENCOUNTER,
        "Annual wellness visit with primary care",
        f"Patient seen on {f(pcp, d_pre)} for annual wellness examination.",
    )
    b.filler(doc, ["history", "ros", "vitals", "exam"])
    b.para(doc, "Assessment: Routine health maintenance. No acute concerns raised today.")
    b.filler(doc, ["plan_generic", "attestation"])

    # ED visit 1
    doc = b.doc(DocType.ED_NOTE, "Emergency Department Provider Note", t0, hosp, ed1_md)
    b.event(
        "ed1_visit",
        doc,
        t0,
        E.ENCOUNTER,
        "Emergency department visit for abdominal pain",
        f"Date of service: {f(hosp, t0)}. Patient presents to the emergency department.",
    )
    b.event(
        "ed1_complaint",
        doc,
        t0,
        E.COMPLAINT,
        "Periumbilical abdominal pain with nausea for about 12 hours",
        "Chief complaint: periumbilical abdominal pain with nausea for approximately 12 hours.",
        prefix="HPI: ",
    )
    b.filler(doc, ["history", "ros", "vitals"])
    b.para(
        doc,
        "Abdominal exam: mild tenderness to deep palpation in the periumbilical region. "
        "No rebound. No guarding documented.",
    )
    b.event(
        "ed1_wbc",
        doc,
        t0,
        E.LAB_RESULT,
        f"White blood cell count {wbc} (high)",
        f"Labs: WBC {wbc} x10^3/uL (H). Lipase within normal limits.",
        tags=("wbc_ed_note",),
    )
    b.event(
        "ed1_dx",
        doc,
        t0,
        E.DIAGNOSIS,
        "Diagnosed with gastritis / nonspecific abdominal pain",
        "Impression: abdominal pain, likely gastritis.",
        tags=("ed1_impression",),
    )
    b.event(
        "ed1_omeprazole",
        doc,
        t0,
        E.MEDICATION,
        "Omeprazole 20 mg daily prescribed",
        "Discharged home with omeprazole 20 mg by mouth daily.",
    )
    b.event(
        "ed1_return_precautions",
        doc,
        t0,
        E.INSTRUCTION,
        "Return precautions for worsening pain or fever",
        "Return precautions given for worsening pain, fever or vomiting.",
    )
    b.filler(doc, ["attestation"])

    # Lab report ED1
    doc = b.doc(DocType.LAB_REPORT, "Laboratory Report - Hematology", t0, hosp, "Laboratory")
    b.cite(
        "ed1_wbc",
        doc,
        f"WBC 4.0-11.0 x10^3/uL  RESULT {wbc}  FLAG H  COLLECTED {f(hosp, t0)}",
    )
    b.para(doc, "HGB 13.1-17.2 g/dL  RESULT 14.2. PLT 150-400 x10^3/uL  RESULT 268.")
    b.para(doc, "Lipase 13-60 U/L  RESULT 31. Specimen received and verified by lab staff.")

    # ED visit 2
    doc = b.doc(DocType.ED_NOTE, "Emergency Department Provider Note", t1, hosp, ed2_md)
    b.event(
        "ed2_visit",
        doc,
        t1,
        E.ENCOUNTER,
        "Return emergency department visit",
        f"Date of service: {f(hosp, t1)}. Patient returns to the emergency department.",
    )
    b.event(
        "ed2_complaint",
        doc,
        t1,
        E.COMPLAINT,
        "Worsening right lower quadrant pain with fever",
        "Worsening right lower quadrant pain with fever to 38.9 C since last evening.",
        prefix="HPI: ",
    )
    b.para(doc, f"Patient reports being seen in this emergency department on {f(hosp, t0)}.")
    b.filler(doc, ["vitals", "exam"])
    b.event(
        "ed2_surg_consult",
        doc,
        t1,
        E.REFERRAL,
        "General surgery consulted",
        "General surgery consulted emergently after CT results.",
    )

    # CT report
    doc = b.doc(
        DocType.RADIOLOGY_REPORT, "CT Abdomen and Pelvis with Contrast", t1, hosp, radiologist
    )
    b.event(
        "ct_appendicitis",
        doc,
        t1,
        E.IMAGING,
        "CT shows perforated appendicitis with abscess",
        "IMPRESSION: Perforated appendicitis with a 3.1 cm periappendiceal abscess.",
    )
    b.para(doc, "Technique: Axial images of the abdomen and pelvis with IV contrast.")
    b.para(doc, "Comparison: None available.")

    # Operative report
    doc = b.doc(DocType.OPERATIVE_REPORT, "Operative Report", t1, hosp, surgeon)
    b.event(
        "appendectomy",
        doc,
        t1,
        E.PROCEDURE,
        "Laparoscopic converted to open appendectomy",
        "Procedure: laparoscopic converted to open appendectomy with drainage of abscess.",
        prefix=f"Date of procedure: {f(hosp, t1)}. ",
        tags=("op_date",),
    )
    b.para(
        doc,
        "Findings: perforated appendix with purulent fluid in the right lower quadrant. "
        "Conversion to open approach due to inflammation obscuring anatomy.",
    )
    b.para(doc, "Estimated blood loss 50 mL. Specimen: appendix to pathology. No complications.")

    # Inpatient progress notes
    b.event(
        "pip_tazo",
        b.doc(DocType.PROGRESS_NOTE, "Hospitalist Progress Note", _shift(t1, 1), hosp, hospitalist),
        _shift(t1, 1),
        E.MEDICATION,
        "IV piperacillin-tazobactam started",
        "Started IV piperacillin-tazobactam 4.5 g every 8 hours.",
        prefix="Plan: ",
    )
    b.filler(b.docs[-1], ["nursing", "vitals"])
    pod3 = _shift(t1, 3)
    doc = b.doc(DocType.PROGRESS_NOTE, "Surgery Progress Note", pod3, hosp, surgeon)
    b.event(
        "ssi",
        doc,
        pod3,
        E.DIAGNOSIS,
        "Surgical site infection of incision",
        "Incision with erythema and purulent drainage consistent with superficial "
        "surgical site infection.",
        prefix="POD 3. ",
    )
    b.para(doc, "Wound opened at bedside and packed. Continue antibiotics.")
    b.filler(doc, ["nursing"])
    for extra in range(4, los):
        d = _shift(t1, extra)
        doc = b.doc(DocType.PROGRESS_NOTE, "Hospitalist Progress Note", d, hosp, hospitalist)
        b.para(doc, f"Hospital day {extra + 1}. Afebrile overnight. Tolerating regular diet.")
        b.filler(doc, ["vitals", "nursing", "exam"])

    # Discharge summary
    doc = b.doc(DocType.DISCHARGE_SUMMARY, "Discharge Summary", t_dc, hosp, hospitalist)
    b.event(
        "discharge",
        doc,
        t_dc,
        E.ENCOUNTER,
        "Discharged from hospital",
        f"Admission date: {f(hosp, t1)}. Discharge date: {f(hosp, t_dc)}.",
    )
    if o.date_conflict:
        wrong = _shift(t1, b.rng.choice([-2, -1, 1, 2]))
        b.para(
            doc,
            f"Hospital course: underwent appendectomy on {f(hosp, wrong)} for perforated "
            "appendicitis.",
            "dc_op_date",
        )
        b.issue(
            IssueKind.DATE_CONFLICT,
            "The discharge summary gives a different appendectomy date than the operative "
            "report. Which date is correct?",
            ["appendectomy"],
            ["op_date", "dc_op_date"],
        )
    else:
        b.cite(
            "appendectomy",
            doc,
            f"Hospital course: underwent appendectomy on {f(hosp, t1)} for perforated "
            "appendicitis.",
        )
    b.event(
        "amox_clav",
        doc,
        t_dc,
        E.MEDICATION,
        "Amoxicillin-clavulanate at discharge",
        "Discharge medications: amoxicillin-clavulanate 875 mg twice daily for 7 days.",
    )
    fu_days = 14
    b.event(
        "surg_followup_referral",
        doc,
        t_dc,
        E.REFERRAL,
        "Follow up in surgery clinic in 2 weeks",
        f"Follow up in general surgery clinic with {surgeon} in 2 weeks.",
        tags=("fu_instruction",),
    )
    b.filler(doc, ["plan_generic"])

    # Planted issue: question about first-visit labs
    b.issue(
        IssueKind.POTENTIAL_DELAY,
        f"The WBC was elevated ({wbc}) at the first ED visit, and the patient was discharged "
        f"with a gastritis impression. The diagnosis of perforated appendicitis came "
        f"{gap_days} days later. Was the elevated WBC addressed before discharge?",
        ["ed1_wbc", "ed1_dx", "ct_appendicitis"],
        ["wbc_ed_note", "ed1_impression"],
    )

    # Surgery clinic follow-up (dropped when planting a missing record)
    d_fu = _shift(t_dc, fu_days + b.rng.randint(-2, 2))
    fu = b.doc(DocType.OFFICE_VISIT, "Surgery Clinic Post-operative Visit", d_fu, hosp, surgeon)
    b.para(fu, f"Post-operative visit on {f(hosp, d_fu)}. Wound healing by secondary intention.")
    if o.missing_record:
        b.remove_doc(fu)
        b.issue(
            IssueKind.MISSING_RECORD,
            "The discharge summary directs a surgery clinic follow-up in about 2 weeks, but no "
            "surgery clinic record appears in the production. Was the visit attended? Request "
            "the records.",
            ["surg_followup_referral"],
            ["fu_instruction"],
        )
    else:
        b.event(
            "surg_followup",
            fu,
            d_fu,
            E.ENCOUNTER,
            "Post-operative surgery clinic visit",
            "Wound packing discontinued. Return as needed.",
        )

    # PCP follow-up
    d_pcp = _shift(t_dc, b.rng.randint(20, 35))
    doc = b.doc(DocType.OFFICE_VISIT, "Office Visit - Hospital Follow-up", d_pcp, pcp, pcp_md)
    b.event(
        "pcp_followup",
        doc,
        d_pcp,
        E.ENCOUNTER,
        "Primary care hospital follow-up",
        f"Seen on {f(pcp, d_pcp)} for follow-up after hospitalization for appendicitis.",
    )
    b.filler(doc, ["ros", "vitals", "exam", "plan_generic", "attestation"])


# --------------------------------------------------------------------------- #
# Scenario 2: knee replacement, anticoagulation change, later DVT/PE          #
# --------------------------------------------------------------------------- #
def knee_replacement_vte(b: CaseBuilder, o: ScenarioOptions) -> None:
    t0 = _day(b)
    hosp = b.facility(names.HOSPITALS)
    ortho = b.facility(names.ORTHO_PRACTICES)
    home = b.facility(names.HOME_HEALTH)
    surgeon = b.clinician("MD")
    hospitalist = b.clinician("DO")
    therapist = b.clinician("PT, DPT")
    ed_md = b.clinician("MD")
    radiologist = b.clinician("MD")
    side = b.rng.choice(["right", "left"])
    f = b.fmt

    d_consult = _shift(t0, -b.rng.randint(30, 60))
    doc = b.doc(DocType.OFFICE_VISIT, "Orthopedic Consultation", d_consult, ortho, surgeon)
    b.event(
        "oa_dx",
        doc,
        d_consult,
        E.DIAGNOSIS,
        f"Severe osteoarthritis of the {side} knee",
        f"Assessment: severe tricompartmental osteoarthritis of the {side} knee.",
    )
    b.filler(doc, ["history", "exam"])
    b.event(
        "tka_plan",
        doc,
        d_consult,
        E.REFERRAL,
        f"Plan for {side} total knee arthroplasty",
        f"Plan: {side} total knee arthroplasty after medical clearance.",
    )
    b.filler(doc, ["plan_generic"])

    doc = b.doc(DocType.OPERATIVE_REPORT, "Operative Report", t0, hosp, surgeon)
    b.event(
        "tka",
        doc,
        t0,
        E.PROCEDURE,
        f"{side.capitalize()} total knee arthroplasty",
        f"Procedure performed: {side} total knee arthroplasty, cemented.",
        prefix=f"Date of surgery: {f(hosp, t0)}. ",
        tags=("op_date",),
    )
    b.para(doc, "Tourniquet time 62 minutes. Estimated blood loss 150 mL. No complications.")

    d1 = _shift(t0, 1)
    doc = b.doc(DocType.PROGRESS_NOTE, "Hospitalist Progress Note", d1, hosp, hospitalist)
    b.event(
        "enoxaparin",
        doc,
        d1,
        E.MEDICATION,
        "Enoxaparin 40 mg daily for VTE prophylaxis",
        "VTE prophylaxis: enoxaparin 40 mg subcutaneously daily, planned for 14 days.",
        tags=("enox_plan",),
    )
    b.filler(doc, ["vitals", "nursing"])

    t_dc = _shift(t0, b.rng.randint(2, 3))
    doc = b.doc(DocType.DISCHARGE_SUMMARY, "Discharge Summary", t_dc, hosp, hospitalist)
    b.event(
        "tka_discharge",
        doc,
        t_dc,
        E.ENCOUNTER,
        "Discharged home after knee replacement",
        f"Discharge date: {f(hosp, t_dc)}. Discharged home with home health services.",
    )
    if o.date_conflict:
        wrong = _shift(t0, b.rng.choice([-1, 1]))
        b.para(doc, f"Underwent {side} total knee arthroplasty on {f(hosp, wrong)}.", "dc_op_date")
        b.issue(
            IssueKind.DATE_CONFLICT,
            "The discharge summary gives a different surgery date than the operative report. "
            "Which date is correct?",
            ["tka"],
            ["op_date", "dc_op_date"],
        )
    else:
        b.cite("tka", doc, f"Underwent {side} total knee arthroplasty on {f(hosp, t0)}.")
    b.event(
        "aspirin",
        doc,
        t_dc,
        E.MEDICATION,
        "Aspirin 81 mg twice daily at discharge",
        "Discharge medications: aspirin 81 mg twice daily. Enoxaparin discontinued.",
        tags=("dc_meds",),
    )
    b.issue(
        IssueKind.RECORD_INCONSISTENCY,
        "A 14-day enoxaparin course was planned, but the discharge medication list stops "
        "enoxaparin and lists aspirin. Is the reason for the change documented?",
        ["enoxaparin", "aspirin"],
        ["enox_plan", "dc_meds"],
    )
    b.filler(doc, ["plan_generic"])

    d_pt = _shift(t0, b.rng.randint(7, 10))
    doc = b.doc(DocType.THERAPY_NOTE, "Home Physical Therapy Visit", d_pt, home, therapist)
    b.event(
        "calf_swelling",
        doc,
        d_pt,
        E.COMPLAINT,
        f"{side.capitalize()} calf pain and swelling reported to therapist",
        f"Patient reports new {side} calf pain and swelling since yesterday.",
        prefix=f"Visit date {f(home, d_pt)}. ",
        tags=("pt_swelling",),
    )
    b.event(
        "pt_advice",
        doc,
        d_pt,
        E.INSTRUCTION,
        "Therapist advised contacting surgeon's office",
        "Therapist advised patient to contact the surgeon's office today; message left with "
        "the orthopedic office by therapist.",
        tags=("pt_call",),
    )
    b.para(doc, "Knee flexion 85 degrees, extension -5 degrees. Gait with rolling walker.")

    d_ed = _shift(d_pt, b.rng.randint(2, 4))
    doc = b.doc(DocType.ED_NOTE, "Emergency Department Provider Note", d_ed, hosp, ed_md)
    b.event(
        "ed_visit",
        doc,
        d_ed,
        E.ENCOUNTER,
        "Emergency department visit for dyspnea and leg swelling",
        f"Date of service: {f(hosp, d_ed)}. Presents with shortness of breath and leg swelling.",
        tags=("ed_date",),
    )
    b.filler(doc, ["vitals", "ros"])
    b.event(
        "heparin",
        doc,
        d_ed,
        E.MEDICATION,
        "Heparin infusion started",
        "Heparin infusion started per weight-based protocol.",
    )

    doc = b.doc(DocType.RADIOLOGY_REPORT, "Venous Doppler Ultrasound", d_ed, hosp, radiologist)
    b.event(
        "dvt",
        doc,
        d_ed,
        E.IMAGING,
        f"Doppler: acute DVT of {side} popliteal vein",
        f"IMPRESSION: Acute deep vein thrombosis of the {side} popliteal vein.",
    )
    doc = b.doc(DocType.RADIOLOGY_REPORT, "CT Angiogram Chest", d_ed, hosp, radiologist)
    b.event(
        "pe",
        doc,
        d_ed,
        E.IMAGING,
        "CT angiogram: segmental pulmonary emboli",
        "IMPRESSION: Acute segmental pulmonary emboli in the right lower lobe.",
    )

    d_dc2 = _shift(d_ed, b.rng.randint(3, 4))
    doc = b.doc(DocType.DISCHARGE_SUMMARY, "Discharge Summary", d_dc2, hosp, hospitalist)
    b.event(
        "apixaban",
        doc,
        d_dc2,
        E.MEDICATION,
        "Apixaban started for DVT/PE",
        "Transitioned to apixaban 10 mg twice daily for 7 days, then 5 mg twice daily.",
    )
    b.filler(doc, ["plan_generic"])

    b.issue(
        IssueKind.POTENTIAL_DELAY,
        f"Calf swelling was reported to the therapist on {d_pt.isoformat()}, and emergency "
        f"evaluation took place on {d_ed.isoformat()}. What happened in between?",
        ["calf_swelling", "ed_visit"],
        ["pt_swelling", "ed_date"],
    )
    if o.missing_record:
        b.issue(
            IssueKind.MISSING_RECORD,
            "The therapist documents a message left with the orthopedic office, but the "
            "production has no orthopedic telephone note or call log for that date. Request "
            "the office phone records.",
            ["pt_advice"],
            ["pt_call"],
        )
    else:
        doc = b.doc(DocType.TELEPHONE_NOTE, "Telephone Encounter", d_pt, ortho, surgeon)
        b.event(
            "ortho_call",
            doc,
            d_pt,
            E.COMMUNICATION,
            "Orthopedic office telephone encounter about calf swelling",
            "Telephone message received from home therapist regarding calf swelling; "
            "patient advised to go to the emergency department.",
        )


# --------------------------------------------------------------------------- #
# Scenario 3: wrist injury, preliminary x-ray read revised by radiology        #
# --------------------------------------------------------------------------- #
def wrist_fracture_overread(b: CaseBuilder, o: ScenarioOptions) -> None:
    t0 = _day(b)
    uc = b.facility(names.URGENT_CARES)
    pcp = b.facility(names.CLINICS)
    ortho = b.facility(names.ORTHO_PRACTICES)
    uc_pa = b.clinician("PA-C")
    radiologist = b.clinician("MD")
    pcp_md = b.clinician("MD")
    surgeon = b.clinician("MD")
    side = b.rng.choice(["right", "left"])
    f = b.fmt

    doc = b.doc(DocType.OFFICE_VISIT, "Urgent Care Visit", t0, uc, uc_pa)
    b.event(
        "uc_visit",
        doc,
        t0,
        E.ENCOUNTER,
        "Urgent care visit after fall",
        f"Seen on {f(uc, t0)} after a fall on an outstretched hand.",
    )
    b.event(
        "wrist_pain",
        doc,
        t0,
        E.COMPLAINT,
        f"{side.capitalize()} wrist pain after fall",
        f"Complains of {side} wrist pain, worse with gripping.",
    )
    b.event(
        "xray_prelim",
        doc,
        t0,
        E.IMAGING,
        "Wrist x-ray, preliminary read negative",
        "Wrist x-ray preliminary read by provider: no acute fracture identified.",
        tags=("prelim",),
    )
    b.event(
        "sprain_dx",
        doc,
        t0,
        E.DIAGNOSIS,
        "Wrist sprain diagnosed",
        f"Assessment: {side} wrist sprain. Placed in a wrist splint.",
    )
    b.event(
        "uc_fu",
        doc,
        t0,
        E.INSTRUCTION,
        "Follow up with primary care in one week",
        "Follow up with primary care in 1 week if not improving.",
    )
    b.filler(doc, ["vitals", "attestation"])

    d_final = _shift(t0, 1)
    doc = b.doc(
        DocType.RADIOLOGY_REPORT, "Radiology Report - Wrist 3 Views", d_final, uc, radiologist
    )
    b.event(
        "xray_final",
        doc,
        d_final,
        E.IMAGING,
        "Final radiology read: nondisplaced scaphoid waist fracture",
        "FINAL REPORT: Nondisplaced fracture of the scaphoid waist. Recommend clinical "
        "correlation and follow-up imaging.",
        prefix=f"Report finalized {f(uc, d_final)}. ",
        tags=("final_read",),
    )

    d_pcp = _shift(t0, b.rng.randint(7, 10))
    doc = b.doc(DocType.OFFICE_VISIT, "Office Visit", d_pcp, pcp, pcp_md)
    b.event(
        "pcp_visit",
        doc,
        d_pcp,
        E.ENCOUNTER,
        "Primary care visit for persistent wrist pain",
        f"Visit date: {f(pcp, d_pcp)}. Persistent wrist pain since a fall.",
    )
    b.para(
        doc, "Tenderness in the anatomic snuffbox noted. Urgent care records not available today."
    )
    b.filler(doc, ["ros", "vitals", "plan_generic"])

    d_call = _shift(t0, b.rng.randint(15, 22))
    doc = b.doc(DocType.TELEPHONE_NOTE, "Telephone Encounter", d_call, uc, uc_pa)
    b.event(
        "notify",
        doc,
        d_call,
        E.COMMUNICATION,
        "Patient notified of final radiology result",
        "Patient notified by phone of final radiology read showing scaphoid fracture.",
        prefix=f"{f(uc, d_call)}: ",
        tags=("notify",),
    )
    b.event(
        "ortho_referral",
        doc,
        d_call,
        E.REFERRAL,
        "Referred to orthopedics",
        "Referral placed to orthopedic hand surgery.",
    )
    b.issue(
        IssueKind.POTENTIAL_DELAY,
        f"The final radiology report on {d_final.isoformat()} identified a scaphoid fracture. "
        f"Patient notification is documented on {d_call.isoformat()}. Is there earlier "
        "documentation of result follow-up?",
        ["xray_final", "notify"],
        ["final_read", "notify"],
    )

    d_ortho = _shift(d_call, b.rng.randint(3, 6))
    doc = b.doc(DocType.OFFICE_VISIT, "Hand Surgery Consultation", d_ortho, ortho, surgeon)
    b.event(
        "displaced",
        doc,
        d_ortho,
        E.IMAGING,
        "Repeat x-ray shows displacement of scaphoid fracture",
        "Repeat radiographs today show interval displacement of the scaphoid waist fracture.",
    )
    b.event(
        "orif_plan",
        doc,
        d_ortho,
        E.REFERRAL,
        "Plan open reduction internal fixation",
        "Plan: open reduction and internal fixation of the scaphoid.",
    )
    b.filler(doc, ["history", "plan_generic"])

    d_or = _shift(d_ortho, b.rng.randint(5, 9))
    doc = b.doc(DocType.OPERATIVE_REPORT, "Operative Report", d_or, ortho, surgeon)
    b.event(
        "orif",
        doc,
        d_or,
        E.PROCEDURE,
        "ORIF scaphoid with headless compression screw",
        "Procedure: open reduction internal fixation of scaphoid with headless compression screw.",
        prefix=f"Date: {f(ortho, d_or)}. ",
        tags=("op_date",),
    )
    b.para(doc, "Fluoroscopy confirmed screw position. No intraoperative complications.")

    d_po = _shift(d_or, b.rng.randint(10, 14))
    doc = b.doc(DocType.OFFICE_VISIT, "Post-operative Visit", d_po, ortho, surgeon)
    b.event(
        "postop",
        doc,
        d_po,
        E.ENCOUNTER,
        "Post-operative hand surgery visit",
        f"Post-operative visit {f(ortho, d_po)}. Incision healing well, sutures removed.",
    )
    if o.date_conflict:
        wrong = _shift(d_or, b.rng.choice([-3, -2, 2, 3]))
        b.para(doc, f"Status post scaphoid ORIF on {f(ortho, wrong)}.", "po_op_date")
        b.issue(
            IssueKind.DATE_CONFLICT,
            "The post-operative note gives a different surgery date than the operative report. "
            "Which date is correct?",
            ["orif"],
            ["op_date", "po_op_date"],
        )
    else:
        b.cite("orif", doc, f"Status post scaphoid ORIF on {f(ortho, d_or)}.")
    if o.missing_record:
        b.issue(
            IssueKind.MISSING_RECORD,
            "The ORIF plan refers to occupational therapy after surgery, but no therapy "
            "records are in the production. Request the occupational therapy records.",
            ["orif_plan"],
            ["ot_plan"],
        )
        b.para(doc, "Begin occupational therapy for range of motion; referral placed.", "ot_plan")


Scenario = Callable[[CaseBuilder, ScenarioOptions], None]

SCENARIOS: dict[str, Scenario] = {
    "abdominal_pain_return_visit": abdominal_pain_return_visit,
    "knee_replacement_vte": knee_replacement_vte,
    "wrist_fracture_overread": wrist_fracture_overread,
}

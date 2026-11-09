"""Name pools for synthetic patients, providers and facilities.

All names are fictional combinations. Every rendered page also carries a
"SYNTHETIC RECORD" banner so generated files cannot be mistaken for real records.
"""

from __future__ import annotations

FIRST_NAMES = [
    "Avery", "Jordan", "Riley", "Casey", "Morgan", "Quinn", "Rowan", "Emerson",
    "Harper", "Sawyer", "Reese", "Dakota", "Finley", "Hayden", "Parker", "Skyler",
]  # fmt: skip

LAST_NAMES = [
    "Testwell", "Samplefield", "Mockridge", "Fauxley", "Placeworth", "Demoson",
    "Synthby", "Trialton", "Examplar", "Fixturer", "Proxley", "Modelson",
]  # fmt: skip

CLINICIAN_LAST = [
    "Okafor", "Lindqvist", "Ramanathan", "Delacroix", "Whitfield", "Castellano",
    "Nakamura", "Abernathy", "Kowalczyk", "Hollingsworth", "Mbeki", "Ferreira",
]  # fmt: skip

CLINICIAN_FIRST_INITIAL = list("ABCDEFGHJKLMNPRSTW")

HOSPITALS = [
    "Lakeview Community Hospital",
    "Northbrook Regional Medical Center",
    "St. Synthetic General Hospital",
]

CLINICS = [
    "Maple Street Family Medicine",
    "Riverbend Primary Care Associates",
    "Cedar Hollow Health Partners",
]

URGENT_CARES = ["QuickCare Urgent Care - Elm Plaza", "FirstStop Walk-In Clinic"]

ORTHO_PRACTICES = ["Summit Orthopedic Group", "Keystone Bone & Joint"]

HOME_HEALTH = ["Harborview Home Health Services", "Bright Path Home Therapy"]

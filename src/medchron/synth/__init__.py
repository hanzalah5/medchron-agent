"""Synthetic medical-record benchmark generator (no real patient data)."""

from medchron.synth.generate import GeneratedCase, generate_case, generate_dataset, write_case
from medchron.synth.models import CaseGold

__all__ = ["CaseGold", "GeneratedCase", "generate_case", "generate_dataset", "write_case"]

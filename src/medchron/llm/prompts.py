"""Versioned prompt registry.

Prompts live as files under ``medchron/prompts/<name>/vN.md`` and are reviewed
and versioned like code. Every LLM call records ``name@vN+sha8``, so each
evaluation result traces back to the exact prompt text that produced it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    text: str

    @property
    def id(self) -> str:
        digest = hashlib.sha256(self.text.encode()).hexdigest()[:8]
        return f"{self.name}@v{self.version}+{digest}"


def load_prompt(name: str, version: int | None = None) -> Prompt:
    folder = resources.files("medchron.prompts") / name
    versions = sorted(
        int(m.group(1))
        for entry in folder.iterdir()
        if (m := re.fullmatch(r"v(\d+)\.md", entry.name))
    )
    if not versions:
        raise FileNotFoundError(f"no prompt versions for {name!r}")
    chosen = version if version is not None else versions[-1]
    if chosen not in versions:
        raise FileNotFoundError(f"{name} has no v{chosen} (available: {versions})")
    text = (folder / f"v{chosen}.md").read_text(encoding="utf-8").strip()
    return Prompt(name=name, version=chosen, text=text)

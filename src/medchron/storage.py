"""Blob storage for uploaded productions.

Files are content-addressed by SHA-256. The local filesystem backs the demo;
a cloud bucket is a drop-in implementation of the same two methods (see the
deployment step). In production the bucket is encrypted with customer-managed
keys and readable only by the API and worker service accounts.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class LocalStorage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(os.environ.get("MEDCHRON_STORAGE_DIR", "var/storage"))

    def put(self, data: bytes, suffix: str = ".pdf") -> str:
        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest[:2]}/{digest}{suffix}"
        path = self.root / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return key

    def get(self, key: str) -> bytes:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise KeyError("invalid storage key")
        return path.read_bytes()

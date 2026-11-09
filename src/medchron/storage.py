"""Blob storage for uploaded productions.

Files are content-addressed by SHA-256. Two backends share one interface:

- ``LocalStorage``: a directory (local development, tests, docker compose)
- ``GCSStorage``: a Google Cloud Storage bucket (Cloud Run). In production the
  bucket uses uniform access, has public access prevention enforced, and can
  be read only by the API and worker service accounts. A customer-managed
  key is optional.

Select one with ``MEDCHRON_STORAGE``: a directory path, or ``gs://bucket``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Protocol


class Storage(Protocol):
    root: Any

    def put(self, data: bytes, suffix: str = ".pdf") -> str: ...

    def get(self, key: str) -> bytes: ...


def _key(data: bytes, suffix: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f"{digest[:2]}/{digest}{suffix}"


class LocalStorage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(os.environ.get("MEDCHRON_STORAGE_DIR", "var/storage"))

    def put(self, data: bytes, suffix: str = ".pdf") -> str:
        key = _key(data, suffix)
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


class GCSStorage:  # pragma: no cover - exercised in the deployed environment
    def __init__(self, bucket: str) -> None:
        from google.cloud import storage  # type: ignore[import-untyped]

        self.root = f"gs://{bucket}"
        self._bucket = storage.Client().bucket(bucket)

    def put(self, data: bytes, suffix: str = ".pdf") -> str:
        key = _key(data, suffix)
        blob = self._bucket.blob(key)
        if not blob.exists():
            blob.upload_from_string(data, content_type="application/pdf", if_generation_match=0)
        return key

    def get(self, key: str) -> bytes:
        if ".." in key or key.startswith("/"):
            raise KeyError("invalid storage key")
        data: bytes = self._bucket.blob(key).download_as_bytes()
        return data


def get_storage() -> Storage:
    target = os.environ.get("MEDCHRON_STORAGE", "")
    if target.startswith("gs://"):
        return GCSStorage(target.removeprefix("gs://").strip("/"))
    return LocalStorage(Path(target) if target else None)

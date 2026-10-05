"""The bucket transfer manifest: a durable record of every object version copied.

One JSON object per line, appended and flushed to disk the moment each copy or
replayed delete marker succeeds:

    {"source_bucket": ..., "target_bucket": ..., "key": ..., "source_version": ...,
     "target_version": ..., "delete_marker": false}

It is two things at once:

  - the version-ID map. A copy creates new version IDs, and application records that
    pin a historical revision (the property registry's s3VersionId) must be rewritten
    from source_version to target_version by the schema-aware data migration;
  - the checkpoint. An interrupted transfer resumes from it: the target may then hold
    exactly what the manifest records and nothing else, and every recorded entry is
    skipped. A target holding anything the manifest does not account for is refused.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    source_bucket: str
    target_bucket: str
    key: str
    source_version: str | None
    target_version: str | None
    delete_marker: bool = False

    @property
    def source_ref(self) -> tuple[str, str | None]:
        return self.key, self.source_version


class Manifest:
    def __init__(self, path: Path, source_bucket: str, target_bucket: str) -> None:
        self.path = path
        self.source_bucket = source_bucket
        self.target_bucket = target_bucket
        self.entries: dict[tuple[str, str | None], Entry] = {}
        if path.exists():
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    entry = Entry(**json.loads(line))
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ManifestError(f"{path}:{number}: not a manifest entry: {exc}") from exc
                if (entry.source_bucket, entry.target_bucket) != (source_bucket, target_bucket):
                    raise ManifestError(f"{path} records a transfer from {entry.source_bucket} to {entry.target_bucket}, not this one")
                self.entries[entry.source_ref] = entry

    def __contains__(self, ref: tuple[str, str | None]) -> bool:
        return ref in self.entries

    def target_versions(self) -> set[tuple[str, str]]:
        return {(entry.key, entry.target_version or "null") for entry in self.entries.values()}

    def target_keys(self) -> set[str]:
        return {entry.key for entry in self.entries.values()}

    def record(self, key: str, source_version: str | None, target_version: str | None, *, delete_marker: bool = False) -> None:
        entry = Entry(self.source_bucket, self.target_bucket, key, source_version, target_version, delete_marker)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(asdict(entry), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.entries[entry.source_ref] = entry

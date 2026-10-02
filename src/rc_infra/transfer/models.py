"""What a transfer found and did, in a form that renders as text or JSON."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    CHECKED = "CHECKED"  # dry run: every check passed, nothing written
    COPIED = "COPIED"
    REFUSED = "REFUSED"  # a check failed before any write
    FAILED = "FAILED"  # writing or verification failed
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True)
class Diff:
    """One field where the source and target configurations disagree."""

    path: str
    source: Any
    target: Any
    blocking: bool = True

    def describe(self) -> str:
        return f"{self.path}:\n  source: {_value(self.source)}\n  target: {_value(self.target)}"


@dataclass
class Counts:
    scanned: int = 0
    written: int = 0
    retries: int = 0
    failed: int = 0
    bytes: int = 0
    versions: int = 0
    source_total: int | None = None
    target_total: int | None = None


@dataclass
class Report:
    kind: str  # "table" or "bucket"
    source: str
    target: str
    apply: bool
    outcome: Outcome = Outcome.CHECKED
    reasons: list[str] = field(default_factory=list)
    diffs: list[Diff] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    counts: Counts = field(default_factory=Counts)
    elapsed_seconds: float = 0.0

    def refuse(self, reason: str) -> Report:
        self.outcome = Outcome.REFUSED
        self.reasons.append(reason)
        return self

    def fail(self, reason: str) -> Report:
        self.outcome = Outcome.FAILED
        self.reasons.append(reason)
        return self

    @property
    def ok(self) -> bool:
        return self.outcome in {Outcome.CHECKED, Outcome.COPIED}

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def to_text(self) -> str:
        mode = "apply" if self.apply else "dry run"
        lines = [f"{self.outcome.value}: {self.kind} transfer ({mode})", f"  source: {self.source}", f"  target: {self.target}"]
        lines.extend(f"  {reason}" for reason in self.reasons)
        blocking = [d for d in self.diffs if d.blocking]
        if blocking:
            lines.append("")
            lines.append("Configurations differ:")
            lines.extend(f"\n{d.describe()}" for d in blocking)
        advisory = [d for d in self.diffs if not d.blocking]
        if advisory:
            lines.append("")
            lines.append("Differences that do not block the copy:")
            lines.extend(f"\n{d.describe()}" for d in advisory)
        if self.notes:
            lines.append("")
            lines.extend(f"note: {note}" for note in self.notes)
        lines.append("")
        counted = [f"{key}={value}" for key, value in asdict(self.counts).items() if value not in (None, 0)]
        lines.append(f"Counts: {', '.join(counted) or 'none'}")
        if self.failures:
            lines.append("Failures:")
            lines.extend(f"  - {failure}" for failure in self.failures)
        lines.append(f"Elapsed: {self.elapsed_seconds:.1f}s")
        if self.outcome is Outcome.REFUSED:
            lines.append("No data was written.")
        return "\n".join(lines)


def _value(value: Any) -> str:
    return "null" if value is None else json.dumps(value, sort_keys=True, default=str)

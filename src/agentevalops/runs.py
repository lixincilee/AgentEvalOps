"""Run and result model: the backbone of version-to-version comparison."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .versioning import VersionManifest

# L1 rules / L2 retrieval / L3 ragas / L4 judge / L5 red-team / L6 performance
LAYERS = ("L1", "L2", "L3", "L4", "L5", "L6")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id(manifest: VersionManifest | None = None, *, moment: str | None = None) -> str:
    stamp = moment or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "nomanifest" if manifest is None else manifest.manifest_id[:8]
    return f"{stamp}-{suffix}"


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    layer: str = "L1"
    category: str = "normal"
    failures: tuple[str, ...] = ()
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    total_tokens: int = 0
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "layer": self.layer,
            "category": self.category,
            "failures": list(self.failures),
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "total_tokens": self.total_tokens,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CaseResult":
        return cls(
            case_id=payload["case_id"],
            passed=bool(payload["passed"]),
            layer=payload.get("layer", "L1"),
            category=payload.get("category", "normal"),
            failures=tuple(payload.get("failures", ())),
            latency_ms=float(payload.get("latency_ms", 0.0)),
            cost_usd=float(payload.get("cost_usd", 0.0)),
            total_tokens=int(payload.get("total_tokens", 0)),
            trace_id=payload.get("trace_id"),
        )


def _pass_rate_by(results: Iterable[CaseResult], attribute: str) -> dict[str, float]:
    buckets: dict[str, list[bool]] = {}
    for result in results:
        buckets.setdefault(getattr(result, attribute), []).append(result.passed)
    return {
        key: sum(outcomes) / len(outcomes) for key, outcomes in sorted(buckets.items())
    }


@dataclass(frozen=True)
class RunMetrics:
    total: int
    passed: int
    failed: int
    pass_rate: float
    cost_usd: float
    total_tokens: int
    cost_per_success_usd: float | None
    by_layer: dict[str, float]
    by_category: dict[str, float]

    @classmethod
    def from_results(cls, results: Iterable[CaseResult]) -> "RunMetrics":
        materialised = list(results)
        total = len(materialised)
        passed = sum(1 for result in materialised if result.passed)
        cost = sum(result.cost_usd for result in materialised)
        return cls(
            total=total,
            passed=passed,
            failed=total - passed,
            pass_rate=(passed / total) if total else 0.0,
            cost_usd=cost,
            total_tokens=sum(result.total_tokens for result in materialised),
            cost_per_success_usd=(cost / passed) if passed else None,
            by_layer=_pass_rate_by(materialised, "layer"),
            by_category=_pass_rate_by(materialised, "category"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "cost_usd": self.cost_usd,
            "total_tokens": self.total_tokens,
            "cost_per_success_usd": self.cost_per_success_usd,
            "by_layer": dict(self.by_layer),
            "by_category": dict(self.by_category),
        }


@dataclass
class Run:
    run_id: str
    started_at: str = field(default_factory=utc_now)
    manifest: VersionManifest | None = None
    finished_at: str | None = None
    results: list[CaseResult] = field(default_factory=list)

    @classmethod
    def start(
        cls, manifest: VersionManifest | None = None, *, run_id: str | None = None
    ) -> "Run":
        return cls(run_id=run_id or new_run_id(manifest), manifest=manifest)

    def add(self, result: CaseResult) -> CaseResult:
        self.results.append(result)
        return result

    def finalize(self) -> RunMetrics:
        self.finished_at = utc_now()
        return self.metrics

    @property
    def metrics(self) -> RunMetrics:
        return RunMetrics.from_results(self.results)

    def filter(
        self,
        *,
        layer: str | None = None,
        category: str | None = None,
        passed: bool | None = None,
    ) -> list[CaseResult]:
        return [
            result
            for result in self.results
            if (layer is None or result.layer == layer)
            and (category is None or result.category == category)
            and (passed is None or result.passed is passed)
        ]

    def failures(self) -> list[CaseResult]:
        return self.filter(passed=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "manifest": None if self.manifest is None else self.manifest.to_dict(),
            "metrics": self.metrics.to_dict(),
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Run":
        manifest_payload = payload.get("manifest")
        return cls(
            run_id=payload["run_id"],
            started_at=payload.get("started_at", utc_now()),
            manifest=(
                None
                if manifest_payload is None
                else VersionManifest.from_dict(manifest_payload)
            ),
            finished_at=payload.get("finished_at"),
            results=[CaseResult.from_dict(item) for item in payload.get("results", ())],
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "Run":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

"""Version manifest: an exact record of what produced an evaluation run.

Without this, "版本对比" degrades into comparing screenshots. Every run stores the
code revision, prompt hash, model id, decoding params, retrieval config hash, and
the hash of the case set that was executed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_ID_LENGTH = 12
UNKNOWN_VERSION = "unknown"
NO_GIT = "nogit"
GIT_TIMEOUT_S = 5.0


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    """Stable JSON: sorted keys, no incidental whitespace, UTF-8 preserved."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def hash_cases(cases: Sequence[Mapping[str, Any]]) -> str:
    """Hash of the case set; key order inside a case does not matter, case order does."""
    return sha256_text(canonical_json([dict(case) for case in cases]))


def git_code_version(repo_dir: Path | None = None) -> str:
    """Short commit hash, suffixed ``-dirty`` when the tree has uncommitted changes."""
    cwd = Path.cwd() if repo_dir is None else repo_dir
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
        if revision.returncode != 0:
            return NO_GIT
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return NO_GIT
    suffix = "-dirty" if status.stdout.strip() else ""
    return revision.stdout.strip() + suffix


@dataclass(frozen=True)
class VersionManifest:
    model_id: str
    prompt_hash: str
    case_set_hash: str
    code_version: str = UNKNOWN_VERSION
    prompt_version: str = "v1"
    case_set_version: str = "v1"
    temperature: float = 0.0
    retrieval_config_hash: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def manifest_id(self) -> str:
        return sha256_text(canonical_json(self.content()))[:MANIFEST_ID_LENGTH]

    @property
    def is_reproducible(self) -> bool:
        """True when the code revision and prompt are pinned to a clean commit."""
        if self.code_version in {NO_GIT, UNKNOWN_VERSION}:
            return False
        return not self.code_version.endswith("-dirty")

    def content(self) -> dict[str, Any]:
        """The pinned fields that define this version's identity (the hash input)."""
        payload = asdict(self)
        payload["extra"] = dict(self.extra)
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload = self.content()
        payload["manifest_id"] = self.manifest_id
        payload["is_reproducible"] = self.is_reproducible
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VersionManifest":
        known = {
            "model_id",
            "prompt_hash",
            "case_set_hash",
            "code_version",
            "prompt_version",
            "case_set_version",
            "temperature",
            "retrieval_config_hash",
            "extra",
        }
        return cls(**{key: payload[key] for key in known if key in payload})


def build_manifest(
    *,
    model_id: str,
    prompt_text: str,
    cases: Sequence[Mapping[str, Any]] = (),
    prompt_version: str = "v1",
    case_set_version: str = "v1",
    temperature: float = 0.0,
    retrieval_config: Mapping[str, Any] | None = None,
    repo_dir: Path | None = None,
    extra: Mapping[str, str] | None = None,
) -> VersionManifest:
    return VersionManifest(
        model_id=model_id,
        prompt_hash=sha256_text(prompt_text),
        case_set_hash=hash_cases(cases),
        code_version=git_code_version(repo_dir),
        prompt_version=prompt_version,
        case_set_version=case_set_version,
        temperature=temperature,
        retrieval_config_hash=(
            None if retrieval_config is None else sha256_text(canonical_json(retrieval_config))
        ),
        extra=dict(extra or {}),
    )

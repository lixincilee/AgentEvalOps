"""Version manifest: hashing, stability, and sensitivity."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from agentevalops.versioning import (
    MANIFEST_ID_LENGTH,
    NO_GIT,
    VersionManifest,
    build_manifest,
    canonical_json,
    git_code_version,
    hash_cases,
    sha256_text,
)

CASES = [
    {"case_id": "a", "message": "查询订单", "category": "normal"},
    {"case_id": "b", "message": "退款", "category": "boundary"},
]


def _manifest(**overrides: object) -> VersionManifest:
    base: dict[str, object] = {
        "model_id": "deepseek-chat",
        "prompt_hash": sha256_text("你是一个客服助手"),
        "case_set_hash": hash_cases(CASES),
    }
    base.update(overrides)
    return VersionManifest(**base)  # type: ignore[arg-type]


def test_sha256_text_is_stable_and_sensitive_to_whitespace():
    assert sha256_text("客服") == sha256_text("客服")
    assert sha256_text("客服") != sha256_text("客服 ")


def test_canonical_json_sorts_keys_recursively():
    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'


def test_hash_cases_ignores_key_order_but_respects_case_order():
    shuffled_keys = [
        {"message": c["message"], "case_id": c["case_id"], "category": c["category"]}
        for c in CASES
    ]
    assert hash_cases(CASES) == hash_cases(shuffled_keys)
    assert hash_cases(CASES) != hash_cases(list(reversed(CASES)))


def test_manifest_id_is_stable_and_has_the_expected_length():
    assert _manifest().manifest_id == _manifest().manifest_id
    assert len(_manifest().manifest_id) == MANIFEST_ID_LENGTH


def test_manifest_id_is_stable_across_processes():
    """Guards against PYTHONHASHSEED- or id()-dependent hashing."""
    code = (
        "from agentevalops.versioning import VersionManifest, hash_cases, sha256_text\n"
        "m = VersionManifest(\n"
        "    model_id='deepseek-chat',\n"
        "    prompt_hash=sha256_text('p'),\n"
        "    case_set_hash=hash_cases([{'case_id': 'a'}]),\n"
        ")\n"
        "print(m.manifest_id)\n"
    )
    observed = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout.strip()
        for seed in ("0", "1", "random")
    }
    assert len(observed) == 1, f"manifest_id 在不同进程间不一致: {observed}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_id", "deepseek-reasoner"),
        ("prompt_hash", "0" * 64),
        ("case_set_hash", "1" * 64),
        ("prompt_version", "v2"),
        ("case_set_version", "v2"),
        ("temperature", 0.7),
        ("retrieval_config_hash", "2" * 64),
        ("extra", {"k": "v"}),
        ("code_version", "abc1234"),
    ],
)
def test_manifest_id_changes_when_any_pinned_field_changes(field: str, value: object):
    assert _manifest().manifest_id != _manifest(**{field: value}).manifest_id


def test_manifest_round_trips_through_json():
    manifest = _manifest(retrieval_config_hash="3" * 64, extra={"note": "smoke"})
    payload = json.loads(json.dumps(manifest.to_dict(), ensure_ascii=False))
    assert VersionManifest.from_dict(payload).manifest_id == manifest.manifest_id


def test_is_reproducible_requires_a_clean_pinned_revision():
    assert _manifest(code_version="deadbee").is_reproducible is True
    assert _manifest(code_version="deadbee-dirty").is_reproducible is False
    assert _manifest(code_version=NO_GIT).is_reproducible is False


def test_build_manifest_derives_every_hash():
    manifest = build_manifest(
        model_id="deepseek-chat",
        prompt_text="prompt",
        cases=CASES,
        retrieval_config={"top_k": 5},
    )
    assert manifest.prompt_hash == sha256_text("prompt")
    assert manifest.case_set_hash == hash_cases(CASES)
    assert manifest.retrieval_config_hash == sha256_text(canonical_json({"top_k": 5}))


def test_retrieval_config_hash_is_none_when_absent():
    assert build_manifest(model_id="m", prompt_text="p").retrieval_config_hash is None


def test_git_code_version_falls_back_outside_a_repository(tmp_path):
    assert git_code_version(tmp_path) == NO_GIT


@pytest.mark.skipif(shutil.which("git") is None, reason="git 未安装")
def test_git_code_version_reports_the_dirty_suffix(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init")
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "init")

    clean = git_code_version(tmp_path)
    assert not clean.endswith("-dirty")
    assert clean != NO_GIT

    (tmp_path / "f.txt").write_text("changed", encoding="utf-8")
    assert git_code_version(tmp_path).endswith("-dirty")

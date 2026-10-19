"""Settings loading and ``.env`` parsing."""

from __future__ import annotations

import pytest

from agentevalops.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ConfigurationError,
    load_env_into_environ,
    load_settings,
    parse_dotenv,
)
from agentevalops.llm.base import MissingCredentialError
from agentevalops.llm.costing import ModelPrice, Usage, estimate_cost


def test_defaults_when_nothing_is_configured(tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env", environ={})
    assert settings.api_key is None
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.model == DEFAULT_MODEL
    assert settings.temperature == 0.0
    assert settings.max_tokens == 1024
    assert settings.budget is None
    assert settings.llm_configured is False


def test_parse_dotenv_handles_quotes_comments_and_export():
    parsed = parse_dotenv(
        "\n".join(
            [
                "# 这是注释",
                "",
                "DEEPSEEK_API_KEY=sk-plain",
                'DEEPSEEK_MODEL="deepseek-reasoner"',
                "export AGENTEVALOPS_TEMPERATURE='0.2'",
                "MALFORMED_LINE",
                "  SPACED_KEY  =  spaced value  ",
            ]
        )
    )
    assert parsed["DEEPSEEK_API_KEY"] == "sk-plain"
    assert parsed["DEEPSEEK_MODEL"] == "deepseek-reasoner"
    assert parsed["AGENTEVALOPS_TEMPERATURE"] == "0.2"
    assert parsed["SPACED_KEY"] == "spaced value"
    assert "MALFORMED_LINE" not in parsed


def test_load_env_into_environ_only_injects_selected_missing_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LANGFUSE_PUBLIC_KEY=pk-file",
                "LANGFUSE_SECRET_KEY=sk-file",
                "LANGFUSE_HOST=https://cloud.langfuse.com",
                "DEEPSEEK_API_KEY=sk-deepseek",
            ]
        ),
        encoding="utf-8",
    )
    environ = {"LANGFUSE_PUBLIC_KEY": "pk-real"}

    loaded = load_env_into_environ(
        env_file,
        environ=environ,
        keys=("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"),
    )

    assert loaded == ["LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"]
    assert environ["LANGFUSE_PUBLIC_KEY"] == "pk-real"  # 真实环境变量优先
    assert environ["LANGFUSE_SECRET_KEY"] == "sk-file"
    assert "DEEPSEEK_API_KEY" not in environ  # 只桥接白名单


def test_load_env_into_environ_tolerates_missing_file(tmp_path):
    environ: dict[str, str] = {}
    assert load_env_into_environ(tmp_path / "missing.env", environ=environ) == []
    assert environ == {}


def test_env_file_is_used_when_the_environment_is_empty(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=from-file\nDEEPSEEK_MODEL=file-model\n", encoding="utf-8"
    )
    settings = load_settings(env_file=env_file, environ={})
    assert settings.api_key == "from-file"
    assert settings.model == "file-model"
    assert settings.llm_configured is True


def test_real_environment_wins_over_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=from-file\n", encoding="utf-8")
    settings = load_settings(env_file=env_file, environ={"DEEPSEEK_API_KEY": "from-env"})
    assert settings.api_key == "from-env"


def test_missing_env_file_is_not_an_error(tmp_path):
    assert load_settings(env_file=tmp_path / "nope.env", environ={}).api_key is None


def test_numeric_settings_are_parsed(tmp_path):
    settings = load_settings(
        env_file=None,
        environ={
            "AGENTEVALOPS_TEMPERATURE": "0.3",
            "AGENTEVALOPS_MAX_TOKENS": "512",
            "AGENTEVALOPS_REQUEST_TIMEOUT_S": "30",
            "AGENTEVALOPS_MAX_RETRIES": "0",
            "AGENTEVALOPS_BUDGET": "1.5",
        },
    )
    assert settings.temperature == 0.3
    assert settings.max_tokens == 512
    assert settings.request_timeout_s == 30.0
    assert settings.max_retries == 0
    assert settings.budget == 1.5


@pytest.mark.parametrize("value", ["abc", "0.1.2", "1,5"])
def test_invalid_temperature_is_reported_clearly(value):
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=None, environ={"AGENTEVALOPS_TEMPERATURE": value})
    assert "AGENTEVALOPS_TEMPERATURE" in str(excinfo.value)


@pytest.mark.parametrize("value", ["many", "3.5"])
def test_invalid_max_tokens_is_reported_clearly(value):
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=None, environ={"AGENTEVALOPS_MAX_TOKENS": value})
    assert "AGENTEVALOPS_MAX_TOKENS" in str(excinfo.value)


def test_require_api_key_raises_a_helpful_error(tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env", environ={})
    with pytest.raises(MissingCredentialError) as excinfo:
        settings.require_api_key()
    message = str(excinfo.value)
    assert "DEEPSEEK_API_KEY" in message
    assert ".env" in message


def test_require_api_key_returns_the_configured_key():
    settings = load_settings(env_file=None, environ={"DEEPSEEK_API_KEY": "sk-test"})
    assert settings.require_api_key() == "sk-test"


def test_blank_api_key_counts_as_unconfigured():
    settings = load_settings(env_file=None, environ={"DEEPSEEK_API_KEY": "   "})
    assert settings.llm_configured is False


def test_dotenv_with_a_utf8_bom_is_read(tmp_path):
    """Windows editors often add a BOM; it must not rename the first key."""
    env_file = tmp_path / ".env"
    env_file.write_bytes("\ufeffDEEPSEEK_API_KEY=sk-bom\n".encode("utf-8"))
    settings = load_settings(env_file=env_file, environ={})
    assert settings.api_key == "sk-bom"


def test_dotenv_with_an_empty_value_is_unconfigured(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    settings = load_settings(env_file=env_file, environ={})
    assert settings.api_key is None
    assert settings.llm_configured is False


def test_pricing_json_is_unset_by_default(tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env", environ={})
    assert settings.pricing_json is None
    assert settings.model_pricing() == {}


def test_pricing_json_is_read_from_the_environment():
    settings = load_settings(
        env_file=None,
        environ={
            "AGENTEVALOPS_PRICING_JSON": (
                '{"deepseek-flash": {"input_per_1m": 0.1,'
                ' "output_per_1m": 0.4}}'
            )
        },
    )
    table = settings.model_pricing()
    assert table == {"deepseek-flash": ModelPrice(0.1, 0.4, None)}
    assert estimate_cost("deepseek-flash", Usage(1_000_000, 0), table) == pytest.approx(0.1)


def test_a_malformed_pricing_json_is_reported_as_a_configuration_error():
    settings = load_settings(
        env_file=None, environ={"AGENTEVALOPS_PRICING_JSON": "{oops}"}
    )
    with pytest.raises(ConfigurationError) as excinfo:
        settings.model_pricing()
    assert "AGENTEVALOPS_PRICING_JSON" in str(excinfo.value)

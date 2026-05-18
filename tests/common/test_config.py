import pytest

from src.common.config import Settings


def _kwargs(**overrides):
    base = dict(
        database_url="postgresql://u:p@h:5432/db",
        gh_token="t",
        openai_api_key="o",
    )
    base.update(overrides)
    return base


def test_settings_constructs_with_required_fields():
    s = Settings(**_kwargs())
    assert s.llm_model == s.openai_model


def test_settings_requires_openai_key_for_llm_and_images():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Settings(
            database_url="postgresql://u:p@h:5432/db",
            gh_token="t",
            openai_api_key=None,
        )


def test_settings_requires_database_url():
    with pytest.raises(ValueError):
        Settings(database_url="", gh_token="t", openai_api_key="o")


def test_from_env_uses_openai_settings(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    s = Settings.from_env()

    assert s.openai_api_key == "o"
    assert s.openai_model == "gpt-test"

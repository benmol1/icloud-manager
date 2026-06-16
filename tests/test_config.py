import pytest

from app.config import Config


def test_dry_run_defaults_true(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert Config().dry_run is True


def test_dry_run_can_be_disabled(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    assert Config().dry_run is False


def test_validate_raises_on_missing_vars():
    c = Config()
    c.icloud_username = ""
    with pytest.raises(EnvironmentError, match="ICLOUD_USERNAME"):
        c.validate()

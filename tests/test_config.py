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


def test_validate_dry_run_does_not_require_smb():
    c = Config()
    c.icloud_username = "me@example.com"
    c.icloud_password = "pw"
    c.dry_run = True
    c.smb_host = ""
    c.smb_share = ""
    c.validate()  # SMB isn't written in a dry run, so it must not be required


def test_validate_live_requires_smb():
    c = Config()
    c.icloud_username = "me@example.com"
    c.icloud_password = "pw"
    c.dry_run = False
    c.smb_host = ""
    c.smb_share = ""
    with pytest.raises(EnvironmentError, match="SMB_HOST"):
        c.validate()

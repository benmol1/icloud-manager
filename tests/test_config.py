import os
import pytest


def test_dry_run_defaults_true():
    os.environ.pop("DRY_RUN", None)
    # Re-import to pick up env state
    import importlib
    import app.config as cfg_module
    importlib.reload(cfg_module)
    assert cfg_module.Config().dry_run is True


def test_dry_run_can_be_disabled():
    os.environ["DRY_RUN"] = "false"
    import importlib
    import app.config as cfg_module
    importlib.reload(cfg_module)
    assert cfg_module.Config().dry_run is False
    os.environ.pop("DRY_RUN", None)


def test_validate_raises_on_missing_vars():
    from app.config import Config
    c = Config()
    c.icloud_username = ""
    with pytest.raises(EnvironmentError, match="ICLOUD_USERNAME"):
        c.validate()

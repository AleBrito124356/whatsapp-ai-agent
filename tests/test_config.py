"""Settings: placeholder detection and the dry-run switch."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings, is_placeholder


def test_env_example_values_are_placeholders():
    for value in (
        "",
        "   ",
        "EAAG_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        "000000000000000",
        "nvapi-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        "change-me",
    ):
        assert is_placeholder(value), value
    for value in ("EAAGm0PZBxyz", "106540352242922", "nvapi-abc123"):
        assert not is_placeholder(value), value


def test_dry_run_is_automatic_without_credentials():
    assert Settings().dry_run is True
    live = Settings(whatsapp_token="EAAGm0PZBxyz", phone_number_id="106540352242922")
    assert live.whatsapp_configured and live.dry_run is False


def test_dry_run_flag_can_force_dry_run_but_not_live():
    forced = Settings.from_env(
        {"WHATSAPP_TOKEN": "EAAGm0PZBxyz", "WHATSAPP_PHONE_NUMBER_ID": "106540352242922", "WHATSAPP_DRY_RUN": "1"}
    )
    assert forced.whatsapp_configured and forced.dry_run
    no_creds = Settings.from_env({"WHATSAPP_DRY_RUN": "0"})
    assert no_creds.dry_run  # nothing to send with


def test_from_env_defaults_and_overrides(tmp_path):
    s = Settings.from_env({"BUSINESS_NAME": "  Salon Uno  ", "DB_PATH": str(tmp_path / "x.sqlite"), "ADMIN_TOKEN": ""})
    assert s.business_name == "Salon Uno"
    assert s.db_path == Path(tmp_path / "x.sqlite")
    assert s.timezone == "America/Panama"
    assert s.admin_enabled is False
    assert Settings.from_env({"ADMIN_TOKEN": "s3cret-token"}).admin_enabled is True

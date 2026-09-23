"""Central configuration, loaded from environment variables.

Every value has a sane default so the app boots for local development without a
full Meta setup. To talk to real WhatsApp users you need WHATSAPP_TOKEN,
WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_APP_SECRET.

Placeholder values copied from ``.env.example`` (``EAAG_XXX...``,
``000000000000000``, ``nvapi-XXX...``) are treated as *not configured*: the app
then runs in **dry-run mode**, where outbound WhatsApp messages are written to a
local outbox table instead of being POSTed to graph.facebook.com.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
KB_DIR = BASE_DIR / "kb"
DATA_DIR = BASE_DIR / "data"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# Exact placeholder strings shipped in .env.example / older docs.
_PLACEHOLDERS = {"change-me", "choose-a-long-random-string", "your-token-here"}


def is_placeholder(value: Optional[str]) -> bool:
    """True for empty values and the dummy values from ``.env.example``."""
    if value is None:
        return True
    v = value.strip()
    if not v:
        return True
    if v.lower() in _PLACEHOLDERS:
        return True
    if "XXXX" in v.upper():
        return True  # EAAG_XXXXXXXX..., nvapi-XXXXXXXX...
    if set(v) <= {"0"}:
        return True  # 000000000000000
    return False


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


@dataclass(frozen=True)
class Settings:
    # --- WhatsApp / Meta Cloud API ---
    whatsapp_token: str = ""
    phone_number_id: str = ""
    verify_token: str = "change-me"
    app_secret: str = ""
    graph_api_version: str = "v21.0"
    # None = automatic (dry-run whenever WhatsApp is not configured).
    dry_run_flag: Optional[bool] = None

    # --- LLM (NVIDIA NIM, OpenAI-compatible) ---
    nvidia_api_key: str = ""
    nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    nim_model: str = "meta/llama-3.3-70b-instruct"

    # --- Business profile (drives the booking flow and the LLM prompt) ---
    business_name: str = "Barbería Studio Norte"
    business_phone: str = "+507 6000-0000"
    business_description: str = "a barbershop in Panama City"
    timezone: str = "America/Panama"

    # --- Staff console ---
    admin_token: str = ""

    # --- Local audio transcription (optional) ---
    whisper_model: str = "base"

    # --- Paths ---
    kb_dir: Path = KB_DIR
    data_dir: Path = DATA_DIR
    db_path_override: Optional[Path] = None

    # ------------------------------------------------------------ builders
    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """Build settings from a mapping (``os.environ`` or a parsed .env)."""

        def get(name: str, default: str = "") -> str:
            value = env.get(name)
            if not isinstance(value, str) or not value.strip():
                return default
            return value.strip()

        db_path = get("DB_PATH")
        return cls(
            whatsapp_token=get("WHATSAPP_TOKEN"),
            phone_number_id=get("WHATSAPP_PHONE_NUMBER_ID"),
            verify_token=get("WHATSAPP_VERIFY_TOKEN", "change-me"),
            app_secret=get("WHATSAPP_APP_SECRET"),
            graph_api_version=get("GRAPH_API_VERSION", "v21.0"),
            dry_run_flag=_parse_bool(env.get("WHATSAPP_DRY_RUN")),
            nvidia_api_key=get("NVIDIA_API_KEY"),
            nim_base_url=get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            nim_model=get("NIM_MODEL", "meta/llama-3.3-70b-instruct"),
            business_name=get("BUSINESS_NAME", "Barbería Studio Norte"),
            business_phone=get("BUSINESS_PHONE", "+507 6000-0000"),
            business_description=get("BUSINESS_DESCRIPTION", "a barbershop in Panama City"),
            timezone=get("BUSINESS_TIMEZONE", "America/Panama"),
            admin_token=get("ADMIN_TOKEN"),
            whisper_model=get("WHISPER_MODEL", "base"),
            db_path_override=Path(db_path) if db_path else None,
        )

    # --------------------------------------------------------- derived values
    @property
    def graph_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"

    @property
    def messages_url(self) -> str:
        return f"{self.graph_base_url}/{self.phone_number_id}/messages"

    @property
    def db_path(self) -> Path:
        return self.db_path_override or self.data_dir / "bookings.sqlite"

    @property
    def schema_path(self) -> Path:
        return self.data_dir / "schema.sql"

    @property
    def whatsapp_configured(self) -> bool:
        """Real credentials present (not empty, not the .env.example dummies)."""
        return not is_placeholder(self.whatsapp_token) and not is_placeholder(self.phone_number_id)

    @property
    def dry_run(self) -> bool:
        """Write outbound messages to the local outbox instead of calling Meta.

        On automatically when WhatsApp is not configured; ``WHATSAPP_DRY_RUN=1``
        forces it on even with real credentials. ``WHATSAPP_DRY_RUN=0`` cannot
        force live mode without credentials (there would be nothing to send with).
        """
        if self.dry_run_flag is True:
            return True
        return not self.whatsapp_configured

    @property
    def llm_configured(self) -> bool:
        return not is_placeholder(self.nvidia_api_key)

    @property
    def signature_enforced(self) -> bool:
        # Without an app secret we cannot verify signatures. We allow this only
        # for local development and warn loudly on every request.
        return bool(self.app_secret)

    @property
    def admin_enabled(self) -> bool:
        return not is_placeholder(self.admin_token)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings from the process environment (plus ``.env`` when present)."""
    from dotenv import load_dotenv

    load_dotenv()
    return Settings.from_env(os.environ)

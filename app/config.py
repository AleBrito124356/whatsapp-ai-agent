"""Central configuration, loaded once from environment variables.

Every value has a sane default so the app boots for local development without a
full Meta setup. The only values you must supply to talk to real WhatsApp users
are WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_APP_SECRET.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
KB_DIR = BASE_DIR / "kb"
DATA_DIR = BASE_DIR / "data"


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else value


@dataclass(frozen=True)
class Settings:
    # --- WhatsApp / Meta Cloud API ---
    whatsapp_token: str
    phone_number_id: str
    verify_token: str
    app_secret: str
    graph_api_version: str

    # --- LLM (NVIDIA NIM, OpenAI-compatible) ---
    nvidia_api_key: str
    nim_base_url: str
    nim_model: str

    # --- Business profile (drives the booking flow and templates) ---
    business_name: str
    business_phone: str
    timezone: str

    # --- Local audio transcription (optional) ---
    whisper_model: str

    # --- Paths ---
    kb_dir: Path = KB_DIR
    data_dir: Path = DATA_DIR

    @property
    def graph_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"

    @property
    def messages_url(self) -> str:
        return f"{self.graph_base_url}/{self.phone_number_id}/messages"

    @property
    def db_path(self) -> Path:
        override = _env("DB_PATH")
        return Path(override) if override else self.data_dir / "bookings.sqlite"

    @property
    def schema_path(self) -> Path:
        return self.data_dir / "schema.sql"

    @property
    def llm_configured(self) -> bool:
        key = self.nvidia_api_key
        return bool(key) and not key.upper().startswith("NVAPI-XXX")

    @property
    def signature_enforced(self) -> bool:
        # Without an app secret we cannot verify signatures. We allow this only
        # for local development and warn loudly at startup.
        return bool(self.app_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        whatsapp_token=_env("WHATSAPP_TOKEN"),
        phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
        verify_token=_env("WHATSAPP_VERIFY_TOKEN", "change-me"),
        app_secret=_env("WHATSAPP_APP_SECRET"),
        graph_api_version=_env("GRAPH_API_VERSION", "v21.0"),
        nvidia_api_key=_env("NVIDIA_API_KEY"),
        nim_base_url=_env("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        nim_model=_env("NIM_MODEL", "meta/llama-3.3-70b-instruct"),
        business_name=_env("BUSINESS_NAME", "Barbería Studio Norte"),
        business_phone=_env("BUSINESS_PHONE", "+507 6000-0000"),
        timezone=_env("BUSINESS_TIMEZONE", "America/Panama"),
        whisper_model=_env("WHISPER_MODEL", "base"),
    )

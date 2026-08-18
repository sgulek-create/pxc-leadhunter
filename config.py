"""PXC-LeadHunter yapılandırması.

Ortam değişkenlerini `.env` dosyasından ve süreç ortamından okur.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

ENV_FILE = Path(__file__).resolve().parent / ".env"

# PXC = Phoenix Contact
COMPANY_SHORT_NAME = "PXC"
COMPANY_NAME = "Phoenix Contact"
COMPANY_WEBSITE = "https://www.phoenixcontact.com"


class ConfigError(RuntimeError):
    """Yapılandırma veya ortam değişkeni hatası."""


class Settings(BaseModel):
    """Uygulama ayarları."""

    openai_api_key: str = Field(..., min_length=8)
    serper_api_key: str = Field(..., min_length=8)
    openai_model: str = "gpt-4o-mini"
    search_country: str = "tr"
    search_language: str = "tr"
    request_timeout: int = Field(default=20, ge=5, le=120)
    max_page_chars: int = Field(default=8000, ge=500, le=50_000)
    user_agent: str = (
        f"PXC-LeadHunter/1.0 ({COMPANY_NAME}; +{COMPANY_WEBSITE}; market-research CLI)"
    )

    @field_validator("openai_api_key", "serper_api_key")
    @classmethod
    def _strip_keys(cls, value: str) -> str:
        cleaned = value.strip()
        placeholders = {
            "sk-your-openai-api-key-here",
            "your-serper-api-key-here",
            "changeme",
            "xxx",
        }
        if cleaned.lower() in placeholders:
            raise ValueError("API anahtarı şablon değeri; gerçek anahtar girilmeli.")
        return cleaned

    @field_validator("search_country", "search_language", "openai_model")
    @classmethod
    def _normalize_short_text(cls, value: str) -> str:
        return value.strip().lower() if value.strip() else value


def _read_env() -> dict[str, str]:
    load_dotenv(dotenv_path=ENV_FILE, override=False)

    mapping = {
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "serper_api_key": os.getenv("SERPER_API_KEY", ""),
        "openai_model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "search_country": os.getenv("SEARCH_COUNTRY", "tr"),
        "search_language": os.getenv("SEARCH_LANGUAGE", "tr"),
        "request_timeout": os.getenv("REQUEST_TIMEOUT", "20"),
        "max_page_chars": os.getenv("MAX_PAGE_CHARS", "8000"),
    }
    return mapping


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Ortam değişkenlerinden doğrulanmış ayar nesnesi döndürür."""
    raw = _read_env()
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        missing_keys: list[str] = []
        for error in exc.errors():
            field = ".".join(str(part) for part in error.get("loc", ()))
            if field == "openai_api_key":
                missing_keys.append("OPENAI_API_KEY")
            elif field == "serper_api_key":
                missing_keys.append("SERPER_API_KEY")

        hint = ""
        if missing_keys:
            unique = ", ".join(dict.fromkeys(missing_keys))
            hint = (
                f" Eksik veya geçersiz anahtarlar: {unique}. "
                ".env.example dosyasını .env olarak kopyalayıp anahtarları doldurun."
            )
        raise ConfigError(f"Yapılandırma doğrulanamadı.{hint}\n{exc}") from exc

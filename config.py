"""PXC-LeadHunter yapılandırması.

Ortam değişkenlerini `.env` dosyasından ve süreç ortamından okur.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
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

    gemini_api_key: str = Field(..., min_length=8)
    serper_api_key: str = Field(..., min_length=8)
    gemini_model: str = "gemini-3.6-flash"
    search_country: str = "tr"
    search_language: str = "tr"
    request_timeout: int = Field(default=20, ge=5, le=120)
    max_page_chars: int = Field(default=8000, ge=500, le=50_000)
    user_agent: str = (
        f"PXC-LeadHunter/1.0 ({COMPANY_NAME}; +{COMPANY_WEBSITE}; market-research CLI)"
    )

    @field_validator("gemini_api_key", "serper_api_key")
    @classmethod
    def _strip_keys(cls, value: str) -> str:
        cleaned = value.strip()
        placeholders = {
            "",
            "your-gemini-api-key-here",
            "your-serper-api-key-here",
            "changeme",
            "xxx",
        }
        if cleaned.lower() in placeholders:
            raise ValueError("Gerçek API anahtarı girilmeli.")
        return cleaned

    @field_validator("search_country", "search_language", "gemini_model")
    @classmethod
    def _normalize_short_text(cls, value: str) -> str:
        return value.strip().lower() if value.strip() else value


def _pick(file_vals: dict[str, str | None], name: str, default: str = "") -> str:
    if name in file_vals:
        return str(file_vals.get(name) or "").strip()
    env_val = (os.getenv(name) or "").strip()
    return env_val or default


def _read_env() -> dict[str, str]:
    """`.env` dosyasını kaynak kabul eder; süreçteki eski şablon değerlerini ezmez."""
    load_dotenv(dotenv_path=ENV_FILE, override=True)
    file_vals = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    return {
        "gemini_api_key": _pick(file_vals, "GEMINI_API_KEY")
        or _pick(file_vals, "GOOGLE_API_KEY"),
        "serper_api_key": _pick(file_vals, "SERPER_API_KEY"),
        "gemini_model": _pick(file_vals, "GEMINI_MODEL", "gemini-3.6-flash"),
        "search_country": _pick(file_vals, "SEARCH_COUNTRY", "tr"),
        "search_language": _pick(file_vals, "SEARCH_LANGUAGE", "tr"),
        "request_timeout": _pick(file_vals, "REQUEST_TIMEOUT", "20"),
        "max_page_chars": _pick(file_vals, "MAX_PAGE_CHARS", "8000"),
    }


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
            if field == "gemini_api_key":
                missing_keys.append("GEMINI_API_KEY")
            elif field == "serper_api_key":
                missing_keys.append("SERPER_API_KEY")

        hint = ""
        if missing_keys:
            unique = ", ".join(dict.fromkeys(missing_keys))
            hint = (
                f" Eksik veya geçersiz anahtarlar: {unique}. "
                "Proje klasöründeki .env dosyasına gerçek anahtarları yazıp kaydedin."
            )
        raise ConfigError(f"Yapılandırma doğrulanamadı.{hint}\n{exc}") from exc


def reload_settings() -> Settings:
    """`.env` değişikliklerini okumak için önbelleği temizler."""
    get_settings.cache_clear()
    return get_settings()

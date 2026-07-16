"""Configurações da API central.

As variáveis de ambiente continuam tendo prioridade. Para manter compatibilidade
com a instalação legada do ERP, as credenciais do Fracttal já utilizadas pelo
projeto permanecem como fallback quando o Render não possui variáveis próprias.
"""
from __future__ import annotations

from dataclasses import dataclass
import os


# Compatibilidade com a instalação legada já distribuída junto ao ERP.
# Variáveis de ambiente sempre prevalecem sobre estes valores.
_LEGACY_FRACTTAL_BASIC_KEY = 'AePKLRL9GClwQ8nFHx'
_LEGACY_FRACTTAL_BASIC_SECRET = 'A6JlU1bvCNhh5sVyC9AdlfGxRqbS4N5O'


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    api_secret_token: str | None
    fracttal_base_url: str
    fracttal_basic_token: str | None
    fracttal_basic_key: str | None
    fracttal_basic_secret: str | None
    connect_timeout: float
    read_timeout: float
    max_retries: int
    max_pages: int
    max_attachments: int
    max_attachment_bytes: int
    max_pdf_pages: int
    attachment_workers: int
    attachment_preview_ttl_seconds: int
    allowed_attachment_hosts: tuple[str, ...]
    document_gemini_api_key: str | None
    document_gemini_model: str
    ocr_with_gemini: bool
    enable_docs: bool


def load_settings() -> Settings:
    document_key = (
        os.getenv("DOCUMENT_GEMINI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or None
    )
    ocr_env = os.getenv("DOCUMENT_OCR_WITH_GEMINI")
    ocr_enabled = bool(document_key) if ocr_env is None else _bool_env(
        "DOCUMENT_OCR_WITH_GEMINI", False
    )
    return Settings(
        api_secret_token=os.getenv("API_SECRET_TOKEN") or None,
        fracttal_base_url=os.getenv(
            "FRACTTAL_BASE_URL", "https://app.fracttal.com/api"
        ).rstrip("/"),
        fracttal_basic_token=os.getenv("FRACTTAL_BASIC_TOKEN") or None,
        fracttal_basic_key=(
            os.getenv("FRACTTAL_BASIC_KEY")
            or os.getenv("FRACTTAL_KEY")
            or _LEGACY_FRACTTAL_BASIC_KEY
        ),
        fracttal_basic_secret=(
            os.getenv("FRACTTAL_BASIC_SECRET")
            or os.getenv("FRACTTAL_SECRET")
            or _LEGACY_FRACTTAL_BASIC_SECRET
        ),
        connect_timeout=_float_env("HTTP_CONNECT_TIMEOUT", 10.0, 1.0, 60.0),
        read_timeout=_float_env("HTTP_READ_TIMEOUT", 45.0, 5.0, 300.0),
        max_retries=_int_env("HTTP_MAX_RETRIES", 3, 0, 6),
        max_pages=_int_env("FRACTTAL_MAX_PAGES", 20, 1, 100),
        max_attachments=_int_env("MAX_ATTACHMENTS_PER_REQUEST", 20, 1, 100),
        max_attachment_bytes=_int_env(
            "MAX_ATTACHMENT_BYTES", 12 * 1024 * 1024, 256 * 1024, 50 * 1024 * 1024
        ),
        max_pdf_pages=_int_env("MAX_PDF_PAGES", 20, 1, 100),
        attachment_workers=_int_env("ATTACHMENT_WORKERS", 3, 1, 4),
        attachment_preview_ttl_seconds=_int_env(
            "ATTACHMENT_PREVIEW_TTL_SECONDS", 14400, 60, 86400
        ),
        allowed_attachment_hosts=_csv_env(
            "ALLOWED_ATTACHMENT_HOSTS",
            "fracttal-fs.s3.amazonaws.com,.amazonaws.com,.fracttal.com,.cloudfront.net",
        ),
        document_gemini_api_key=document_key,
        document_gemini_model=os.getenv(
            "DOCUMENT_GEMINI_MODEL", "gemini-2.5-flash-lite"
        ).strip(),
        ocr_with_gemini=ocr_enabled,
        enable_docs=_bool_env("ENABLE_API_DOCS", False),
    )

"""Assinatura temporária de URLs de visualização de anexos."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote

from .errors import IntegrationError
from .settings import Settings


def _preview_secret(settings: Settings) -> bytes:
    secret = (settings.api_secret_token or "").strip()
    if not secret:
        raise IntegrationError(
            "preview_not_configured",
            "A API central não possui segredo configurado para gerar a visualização do anexo.",
            status_code=503,
        )
    return secret.encode("utf-8")


def _payload(code: str, attachment_id: int, expires: int) -> bytes:
    return f"{code}:{int(attachment_id)}:{int(expires)}".encode("utf-8")


def sign_preview(
    code: str,
    attachment_id: int,
    settings: Settings,
    *,
    now: int | None = None,
) -> tuple[int, str]:
    current = int(time.time() if now is None else now)
    expires = current + settings.attachment_preview_ttl_seconds
    signature = hmac.new(
        _preview_secret(settings),
        _payload(code, attachment_id, expires),
        hashlib.sha256,
    ).hexdigest()
    return expires, signature


def verify_preview(
    code: str,
    attachment_id: int,
    expires: int,
    signature: str,
    settings: Settings,
    *,
    now: int | None = None,
) -> None:
    current = int(time.time() if now is None else now)
    if int(expires) < current:
        raise IntegrationError(
            "attachment_preview_expired",
            "O link temporário de visualização do anexo expirou.",
            status_code=410,
        )
    expected = hmac.new(
        _preview_secret(settings),
        _payload(code, attachment_id, expires),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, str(signature or "")):
        raise IntegrationError(
            "attachment_preview_invalid_signature",
            "A assinatura do link de visualização do anexo é inválida.",
            status_code=403,
        )


def build_preview_path(code: str, attachment_id: int, settings: Settings) -> str:
    expires, signature = sign_preview(code, attachment_id, settings)
    encoded_code = quote(str(code), safe="")
    return (
        f"/api/fracttal/solicitacoes/{encoded_code}/anexos/{int(attachment_id)}/visualizacao"
        f"?expires={expires}&signature={signature}"
    )

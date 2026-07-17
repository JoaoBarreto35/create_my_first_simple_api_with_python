"""Assinatura temporária de URLs de visualização e download de anexos."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote

from .errors import IntegrationError
from .settings import Settings

_PREVIEW_PURPOSE = "preview"
_DOWNLOAD_PURPOSE = "download"


def _preview_secret(settings: Settings) -> bytes:
    secret = (settings.api_secret_token or "").strip()
    if not secret:
        raise IntegrationError(
            "preview_not_configured",
            "A API central não possui segredo configurado para gerar o acesso temporário ao anexo.",
            status_code=503,
        )
    return secret.encode("utf-8")


def _payload(
    code: str,
    attachment_id: int,
    expires: int,
    purpose: str,
) -> bytes:
    return (
        f"{purpose}:{code}:{int(attachment_id)}:{int(expires)}"
    ).encode("utf-8")


def _sign_access(
    code: str,
    attachment_id: int,
    settings: Settings,
    *,
    purpose: str,
    now: int | None = None,
) -> tuple[int, str]:
    current = int(time.time() if now is None else now)
    expires = current + settings.attachment_preview_ttl_seconds
    signature = hmac.new(
        _preview_secret(settings),
        _payload(code, attachment_id, expires, purpose),
        hashlib.sha256,
    ).hexdigest()
    return expires, signature


def _verify_access(
    code: str,
    attachment_id: int,
    expires: int,
    signature: str,
    settings: Settings,
    *,
    purpose: str,
    now: int | None = None,
) -> None:
    current = int(time.time() if now is None else now)
    if int(expires) < current:
        raise IntegrationError(
            "attachment_access_expired",
            "O link temporário do anexo expirou.",
            status_code=410,
        )
    expected = hmac.new(
        _preview_secret(settings),
        _payload(code, attachment_id, expires, purpose),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, str(signature or "")):
        raise IntegrationError(
            "attachment_access_invalid_signature",
            "A assinatura do link temporário do anexo é inválida.",
            status_code=403,
        )


def sign_preview(
    code: str,
    attachment_id: int,
    settings: Settings,
    *,
    now: int | None = None,
) -> tuple[int, str]:
    return _sign_access(
        code,
        attachment_id,
        settings,
        purpose=_PREVIEW_PURPOSE,
        now=now,
    )


def verify_preview(
    code: str,
    attachment_id: int,
    expires: int,
    signature: str,
    settings: Settings,
    *,
    now: int | None = None,
) -> None:
    _verify_access(
        code,
        attachment_id,
        expires,
        signature,
        settings,
        purpose=_PREVIEW_PURPOSE,
        now=now,
    )


def verify_download(
    code: str,
    attachment_id: int,
    expires: int,
    signature: str,
    settings: Settings,
    *,
    now: int | None = None,
) -> None:
    _verify_access(
        code,
        attachment_id,
        expires,
        signature,
        settings,
        purpose=_DOWNLOAD_PURPOSE,
        now=now,
    )


def build_preview_path(code: str, attachment_id: int, settings: Settings) -> str:
    expires, signature = sign_preview(code, attachment_id, settings)
    encoded_code = quote(str(code), safe="")
    return (
        f"/api/fracttal/solicitacoes/{encoded_code}/anexos/{int(attachment_id)}/visualizacao"
        f"?expires={expires}&signature={signature}"
    )


def build_download_path(code: str, attachment_id: int, settings: Settings) -> str:
    expires, signature = _sign_access(
        code,
        attachment_id,
        settings,
        purpose=_DOWNLOAD_PURPOSE,
    )
    encoded_code = quote(str(code), safe="")
    return (
        f"/api/fracttal/solicitacoes/{encoded_code}/anexos/{int(attachment_id)}/download"
        f"?expires={expires}&signature={signature}"
    )

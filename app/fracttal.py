"""Cliente especializado da API do Fracttal."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import quote

import requests

from .errors import IntegrationError
from .http import build_session
from .settings import Settings

_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_BASIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_STAGE = "consultar_anexos_fracttal"


def normalize_fracttal_authorization(value: str | None) -> str | None:
    """Normaliza uma credencial Basic recebida do ERP.

    O header é opcional. Quando presente, ele prevalece somente nesta chamada e
    nunca é persistido em arquivo ou variável de ambiente.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if len(raw) > 4096 or "\r" in raw or "\n" in raw:
        raise IntegrationError(
            "invalid_fracttal_authorization",
            "A credencial do Fracttal recebida pelo ERP possui formato inválido.",
            status_code=422,
            stage=_STAGE,
        )
    token = raw[6:].strip() if raw.lower().startswith("basic ") else raw
    if not token or not _BASIC_TOKEN_RE.fullmatch(token):
        raise IntegrationError(
            "invalid_fracttal_authorization",
            "A credencial do Fracttal recebida pelo ERP possui formato inválido.",
            status_code=422,
            stage=_STAGE,
        )
    try:
        decoded = base64.b64decode(token, validate=True)
    except (ValueError, TypeError) as exc:
        raise IntegrationError(
            "invalid_fracttal_authorization",
            "A credencial do Fracttal recebida pelo ERP possui formato inválido.",
            status_code=422,
            stage=_STAGE,
        ) from exc
    if b":" not in decoded:
        raise IntegrationError(
            "invalid_fracttal_authorization",
            "A credencial do Fracttal recebida pelo ERP possui formato inválido.",
            status_code=422,
            stage=_STAGE,
        )
    return f"Basic {token}"


@dataclass(frozen=True)
class AttachmentMetadata:
    id: int
    id_request: int
    description: str
    signed_url: str

    def public_dict(self, include_signed_url: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "id_request": self.id_request,
            "description": self.description,
            "download_disponivel": bool(self.signed_url),
        }
        if include_signed_url:
            data["signed_path_image"] = self.signed_url
        return data


class FracttalClient:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        authorization_override: str | None = None,
    ):
        self.settings = settings
        self.session = session or build_session(settings)
        self.authorization_override = normalize_fracttal_authorization(
            authorization_override
        )

    def close(self) -> None:
        self.session.close()

    def _authorization_value(
        self,
        *,
        code: str = "",
        endpoint: str = "work_requests_attachments/{code}",
    ) -> str:
        if self.authorization_override:
            return self.authorization_override
        token = (self.settings.fracttal_basic_token or "").strip()
        if token:
            return token if token.lower().startswith("basic ") else f"Basic {token}"
        key = (self.settings.fracttal_basic_key or "").strip()
        secret = (self.settings.fracttal_basic_secret or "").strip()
        if key and secret:
            encoded = base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("ascii")
            return f"Basic {encoded}"
        raise IntegrationError(
            "fracttal_not_configured",
            (
                "ERRO DE CONFIGURAÇÃO DA API CENTRAL — a consulta ao Fracttal "
                "não foi iniciada porque a autenticação necessária não estava disponível."
            ),
            status_code=503,
            stage=_STAGE,
            request_code=code or None,
            endpoint=endpoint,
        )

    @staticmethod
    def _validate_code(code: str) -> str:
        normalized = str(code).strip()
        if not _CODE_RE.fullmatch(normalized):
            raise IntegrationError(
                "invalid_request_code",
                "O code da solicitação possui formato inválido.",
                status_code=422,
                stage=_STAGE,
                request_code=normalized or None,
            )
        return normalized

    @staticmethod
    def _error_from_status(
        status: int,
        *,
        code: str,
        endpoint: str,
    ) -> IntegrationError:
        if status == 401:
            return IntegrationError(
                "fracttal_authentication_error",
                (
                    f"ERRO DE INTEGRAÇÃO — o Fracttal respondeu HTTP 401 ao "
                    f"consultar os anexos do chamado {code}. As credenciais foram recusadas."
                ),
                upstream_status=status,
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            )
        if status == 403:
            return IntegrationError(
                "fracttal_permission_error",
                (
                    f"ERRO DE INTEGRAÇÃO — o Fracttal respondeu HTTP 403 ao "
                    f"consultar os anexos do chamado {code}. Verifique a permissão "
                    "e o ADD-ON de API Avançada."
                ),
                upstream_status=status,
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            )
        if status == 404:
            return IntegrationError(
                "fracttal_request_not_found",
                (
                    f"ERRO DE INTEGRAÇÃO — o Fracttal respondeu HTTP 404 para o "
                    f"code {code}. A solicitação não foi localizada ou o identificador "
                    "enviado não corresponde ao campo code."
                ),
                status_code=404,
                upstream_status=status,
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            )
        if status == 429:
            return IntegrationError(
                "fracttal_rate_limit",
                (
                    f"ERRO DE INTEGRAÇÃO — o Fracttal respondeu HTTP 429 ao "
                    f"consultar os anexos do chamado {code}. O limite de requisições "
                    "foi atingido."
                ),
                upstream_status=status,
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            )
        if status in (500, 502, 503, 504):
            return IntegrationError(
                "fracttal_unavailable",
                (
                    f"ERRO DE INTEGRAÇÃO — o Fracttal respondeu HTTP {status} ao "
                    f"consultar os anexos do chamado {code}. O serviço está indisponível."
                ),
                upstream_status=status,
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            )
        return IntegrationError(
            "fracttal_upstream_error",
            (
                f"ERRO DE INTEGRAÇÃO — o Fracttal respondeu HTTP {status} ao "
                f"consultar os anexos do chamado {code}."
            ),
            upstream_status=status,
            stage=_STAGE,
            request_code=code,
            endpoint=endpoint,
        )

    def _get_attachments(
        self,
        *,
        code: str,
        cursor: int,
        limit: int,
    ) -> tuple[requests.Response, str]:
        """Consulta exclusivamente o endpoint documentado, usando o campo code."""
        encoded = quote(code, safe="")
        endpoint = f"work_requests_attachments/{encoded}"
        url = f"{self.settings.fracttal_base_url}/{endpoint}"
        try:
            response = self.session.get(
                url,
                headers={"Authorization": self._authorization_value(code=code, endpoint=endpoint)},
                params={"start": cursor, "limit": limit},
                timeout=(self.settings.connect_timeout, self.settings.read_timeout),
            )
        except requests.Timeout as exc:
            raise IntegrationError(
                "fracttal_timeout",
                (
                    f"ERRO DE INTEGRAÇÃO — a consulta dos anexos do chamado {code} "
                    "excedeu o tempo limite."
                ),
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            ) from exc
        except requests.RequestException as exc:
            raise IntegrationError(
                "fracttal_connection_error",
                (
                    f"ERRO DE INTEGRAÇÃO — não foi possível conectar ao Fracttal "
                    f"para consultar os anexos do chamado {code}: {exc.__class__.__name__}."
                ),
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            ) from exc
        return response, endpoint

    @staticmethod
    def _decode_payload(
        response: requests.Response,
        *,
        code: str,
        endpoint: str,
    ) -> dict[str, Any]:
        if response.status_code >= 400:
            raise FracttalClient._error_from_status(
                response.status_code,
                code=code,
                endpoint=endpoint,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise IntegrationError(
                "fracttal_invalid_json",
                (
                    f"ERRO DE INTEGRAÇÃO — o Fracttal respondeu à consulta de "
                    f"anexos do chamado {code}, mas o conteúdo não é um JSON válido."
                ),
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            ) from exc
        if not isinstance(payload, dict):
            raise IntegrationError(
                "fracttal_invalid_payload",
                (
                    f"ERRO DE INTEGRAÇÃO — o Fracttal respondeu à consulta de "
                    f"anexos do chamado {code}, mas o conteúdo não possui o formato esperado."
                ),
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            )
        if payload.get("success") is False:
            detail = str(payload.get("message") or "a consulta foi recusada")
            raise IntegrationError(
                "fracttal_rejected_request",
                (
                    f"ERRO DE INTEGRAÇÃO — o Fracttal recusou a consulta de anexos "
                    f"do chamado {code}: {detail}."
                ),
                stage=_STAGE,
                request_code=code,
                endpoint=endpoint,
            )
        return payload

    def list_request_attachments(
        self,
        code: str,
        *,
        start: int = 0,
        limit: int = 100,
        paginate_all: bool = True,
    ) -> tuple[list[AttachmentMetadata], int]:
        """Consulta os anexos de uma única solicitação pelo campo ``code``.

        Não há consulta geral de anexos, resolução de ID interno ou fallback por
        query string. Uma lista vazia só é retornada quando o Fracttal responde
        com sucesso e ``data`` está vazio.
        """
        code = self._validate_code(code)
        start = max(0, int(start))
        limit = max(1, min(100, int(limit)))
        collected: list[AttachmentMetadata] = []
        seen_ids: set[int] = set()
        source_total = 0
        cursor = start

        for _page in range(self.settings.max_pages):
            response, endpoint = self._get_attachments(
                code=code,
                cursor=cursor,
                limit=limit,
            )
            payload = self._decode_payload(
                response,
                code=code,
                endpoint=endpoint,
            )
            raw_data = payload.get("data", [])
            if raw_data is None:
                raw_data = []
            if not isinstance(raw_data, list):
                raise IntegrationError(
                    "fracttal_invalid_payload",
                    (
                        f"ERRO DE INTEGRAÇÃO — o campo data da consulta de anexos "
                        f"do chamado {code} não possui o formato esperado."
                    ),
                    stage=_STAGE,
                    request_code=code,
                    endpoint=endpoint,
                )
            try:
                source_total = max(
                    source_total,
                    int(payload.get("total", len(raw_data)) or 0),
                )
            except (TypeError, ValueError):
                source_total = max(source_total, len(raw_data))

            for item in raw_data:
                if not isinstance(item, dict):
                    continue
                try:
                    attachment_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                try:
                    request_id = int(item.get("id_request") or code)
                except (TypeError, ValueError):
                    request_id = 0
                signed_url = str(item.get("signed_path_image") or "").strip()
                description = str(
                    item.get("description") or f"anexo-{attachment_id}"
                ).strip()
                if attachment_id in seen_ids:
                    continue
                seen_ids.add(attachment_id)
                collected.append(
                    AttachmentMetadata(
                        id=attachment_id,
                        id_request=request_id,
                        description=description[:255],
                        signed_url=signed_url,
                    )
                )
                if len(collected) >= self.settings.max_attachments:
                    return collected, source_total

            if not paginate_all:
                break
            if not raw_data or (source_total and cursor + len(raw_data) >= source_total):
                break
            if len(raw_data) < limit:
                break
            cursor += len(raw_data)

        return collected, source_total

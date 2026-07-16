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
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or build_session(settings)

    def close(self) -> None:
        self.session.close()

    def _authorization_value(self) -> str:
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
            "Credenciais do Fracttal não foram configuradas na API central.",
            status_code=503,
        )

    @staticmethod
    def _validate_code(code: str) -> str:
        normalized = str(code).strip()
        if not _CODE_RE.fullmatch(normalized):
            raise IntegrationError(
                "invalid_request_code",
                "O code da solicitação possui formato inválido.",
                status_code=422,
            )
        return normalized

    def list_request_attachments(
        self,
        code: str,
        *,
        start: int = 0,
        limit: int = 100,
        paginate_all: bool = True,
    ) -> tuple[list[AttachmentMetadata], int]:
        code = self._validate_code(code)
        start = max(0, int(start))
        limit = max(1, min(100, int(limit)))
        url = (
            f"{self.settings.fracttal_base_url}/"
            f"work_requests_attachments/{quote(code, safe='')}"
        )
        headers = {"Authorization": self._authorization_value()}
        collected: list[AttachmentMetadata] = []
        seen_ids: set[int] = set()
        source_total = 0
        cursor = start

        for _page in range(self.settings.max_pages):
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    params={"start": cursor, "limit": limit},
                    timeout=(self.settings.connect_timeout, self.settings.read_timeout),
                )
            except requests.Timeout as exc:
                raise IntegrationError(
                    "fracttal_timeout",
                    "O Fracttal não respondeu a tempo ao consultar anexos.",
                ) from exc
            except requests.RequestException as exc:
                raise IntegrationError(
                    "fracttal_connection_error",
                    "Não foi possível conectar à API do Fracttal.",
                ) from exc

            if response.status_code in (401, 403):
                raise IntegrationError(
                    "fracttal_authentication_error",
                    "O Fracttal recusou as credenciais da API central.",
                    upstream_status=response.status_code,
                )
            if response.status_code == 404:
                raise IntegrationError(
                    "fracttal_request_not_found",
                    "A solicitação informada não foi localizada no Fracttal.",
                    status_code=404,
                    upstream_status=404,
                )
            if response.status_code >= 400:
                raise IntegrationError(
                    "fracttal_upstream_error",
                    "O Fracttal retornou erro ao consultar anexos.",
                    upstream_status=response.status_code,
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise IntegrationError(
                    "fracttal_invalid_json",
                    "O Fracttal retornou uma resposta que não é JSON válido.",
                ) from exc

            if not isinstance(payload, dict):
                raise IntegrationError(
                    "fracttal_invalid_payload",
                    "O formato da resposta de anexos do Fracttal é inválido.",
                )
            if payload.get("success") is False:
                raise IntegrationError(
                    "fracttal_rejected_request",
                    str(payload.get("message") or "O Fracttal recusou a consulta de anexos."),
                )

            raw_data = payload.get("data", [])
            if raw_data is None:
                raw_data = []
            if not isinstance(raw_data, list):
                raise IntegrationError(
                    "fracttal_invalid_payload",
                    "O campo data da consulta de anexos não é uma lista.",
                )
            try:
                source_total = max(source_total, int(payload.get("total", len(raw_data)) or 0))
            except (TypeError, ValueError):
                source_total = max(source_total, len(raw_data))

            valid_in_page = 0
            for item in raw_data:
                if not isinstance(item, dict):
                    continue
                try:
                    attachment_id = int(item.get("id"))
                    request_id = int(item.get("id_request"))
                except (TypeError, ValueError):
                    continue
                signed_url = str(item.get("signed_path_image") or "").strip()
                description = str(item.get("description") or f"anexo-{attachment_id}").strip()
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
                valid_in_page += 1
                if len(collected) >= self.settings.max_attachments:
                    return collected, source_total

            if not paginate_all:
                break
            if not raw_data or (source_total and cursor + len(raw_data) >= source_total):
                break
            if len(raw_data) < limit:
                break
            cursor += len(raw_data)
            if valid_in_page == 0 and not raw_data:
                break

        return collected, source_total

"""Erros estruturados da integração externa."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IntegrationError(Exception):
    error_type: str
    message: str
    status_code: int = 502
    upstream_status: int | None = None
    stage: str | None = None
    request_code: str | None = None
    endpoint: str | None = None

    def __str__(self) -> str:
        return self.message

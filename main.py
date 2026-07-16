from __future__ import annotations

"""API central do ERP de manutenção.

Compatibilidade preservada:
- GET /api/bridge
- GET /api/executar
- GET /check_health

Novos endpoints especializados:
- GET /api/fracttal/solicitacoes/{code}/anexos
- GET /api/fracttal/solicitacoes/{code}/anexos/processados

Os endpoints especializados usam credenciais do Fracttal armazenadas no Render,
tratam paginação e erros de forma explícita e nunca confundem falha de
integração com ausência real de anexos.
"""

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import requests

from app.documents import process_attachments
from app.errors import IntegrationError
from app.fracttal import FracttalClient
from app.http import build_session
from app.security import require_api_token, validate_fracttal_bridge_url
from app.settings import Settings, load_settings

settings: Settings = load_settings()
security = HTTPBearer(auto_error=False)

app = FastAPI(
    title="ERP Manutenção — API Central",
    version="2.0.0",
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    response.headers[
        "Content-Security-Policy"
    ] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(IntegrationError)
async def integration_error_handler(_request: Request, exc: IntegrationError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error_type": exc.error_type,
            "message": exc.message,
            "upstream_status": exc.upstream_status,
            "data": [],
        },
    )


def _authorize(credentials: HTTPAuthorizationCredentials | None) -> None:
    require_api_token(credentials, settings)


def _legacy_gemini_execute(
    api_key: str,
    modelo: str,
    contexto_classificacao: str,
    texto: str,
) -> tuple[bool, str]:
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        chat = client.chats.create(
            model=modelo,
            config=types.GenerateContentConfig(
                system_instruction=contexto_classificacao,
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        resposta = chat.send_message(texto)
        return True, str(resposta.text or "")
    except Exception as exc:
        return False, str(exc)


def _legacy_bridge(url: str, token: str) -> list[Any]:
    """Mantém o contrato legado do ERP sem esconder URLs externas livres.

    A resposta continua sendo uma lista no campo ``resultado``. Os novos
    endpoints especializados são os que oferecem erro estruturado.
    """
    validate_fracttal_bridge_url(url, settings)
    headers = {"Authorization": f"Basic {token}"}
    session = build_session(settings)
    try:
        response = session.get(
            url,
            headers=headers,
            timeout=(settings.connect_timeout, settings.read_timeout),
        )
        if response.status_code != 200:
            return []
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return data if isinstance(data, list) else []
    except (requests.RequestException, ValueError):
        return []
    finally:
        session.close()


@app.get("/health")
def public_health() -> dict[str, str]:
    """Health check público, sem segredos ou acesso a integrações externas."""
    return {"status": "ok", "service": "erp-central-api", "version": "2.0.0"}


@app.get("/check_health")
def check_health_api(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    _authorize(credentials)
    return {"status": "ok", "description": "Api check health", "version": "2.0.0"}


@app.get("/api/executar")
def executar_funcao(
    api_key: str,
    modelo: str,
    CONTEXTO_CLASSIFICACAO: str,
    texto: str,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    """Endpoint legado do Gemini, preservado sem mudança de contrato."""
    _authorize(credentials)
    resultado = _legacy_gemini_execute(
        api_key,
        modelo,
        CONTEXTO_CLASSIFICACAO,
        texto,
    )
    return {"resultado": resultado}


@app.get("/api/bridge")
def executar_bridge(
    url: str,
    token: str,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    """Bridge legado restrito à API oficial do Fracttal."""
    _authorize(credentials)
    return {"resultado": _legacy_bridge(url, token)}


@app.get("/api/fracttal/solicitacoes/{code}/anexos")
@app.get(
    "/api/fracttal/work-requests/{code}/attachments",
    include_in_schema=False,
)
def consultar_anexos_solicitacao(
    code: str,
    start: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    paginate_all: bool = Query(True),
    include_signed_url: bool = Query(False),
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    """Consulta metadados dos anexos de uma solicitação.

    Por segurança, URLs assinadas não são devolvidas por padrão. O endpoint de
    processamento baixa os arquivos internamente e retorna apenas texto,
    metadados e status de extração necessários ao ERP.
    """
    _authorize(credentials)
    client = FracttalClient(settings)
    try:
        attachments, source_total = client.list_request_attachments(
            code,
            start=start,
            limit=limit,
            paginate_all=paginate_all,
        )
    finally:
        client.close()
    return {
        "success": True,
        "message": "Anexos consultados com sucesso.",
        "code": code,
        "total": len(attachments),
        "source_total": source_total,
        "data": [item.public_dict(include_signed_url) for item in attachments],
    }


@app.get("/api/fracttal/solicitacoes/{code}/anexos/processados")
@app.get(
    "/api/fracttal/work-requests/{code}/attachments/processed",
    include_in_schema=False,
)
def processar_anexos_solicitacao(
    code: str,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    """Baixa e extrai os anexos para a validação documental no ERP.

    O retorno usa a chave ``anexos_autorizacao`` esperada pelo ERP_22. Lista
    vazia significa que a consulta foi executada e nenhum anexo existe. Falhas
    de autenticação, rede ou formato retornam erro HTTP estruturado e nunca uma
    lista vazia enganosa.
    """
    _authorize(credentials)
    client = FracttalClient(settings)
    try:
        attachments, source_total = client.list_request_attachments(
            code,
            start=0,
            limit=100,
            paginate_all=True,
        )
    finally:
        client.close()
    extracted = process_attachments(attachments, settings)
    payload = [item.to_erp_dict() for item in extracted]
    extracted_count = sum(1 for item in extracted if item.status_extracao == "EXTRAIDO")
    error_count = sum(1 for item in extracted if item.status_extracao == "ERRO_INTEGRACAO")
    manual_count = len(extracted) - extracted_count - error_count

    if not attachments:
        processing_status = "SEM_ANEXOS"
    elif error_count == len(extracted):
        processing_status = "ERRO_INTEGRACAO"
    elif error_count or manual_count:
        processing_status = "PARCIAL"
    else:
        processing_status = "CONCLUIDO"

    return {
        "success": True,
        "message": "Anexos processados para validação documental.",
        "code": code,
        "total": len(attachments),
        "source_total": source_total,
        "processing_status": processing_status,
        "anexos_extraidos": extracted_count,
        "anexos_validacao_manual": manual_count,
        "anexos_com_erro": error_count,
        "anexos_autorizacao": payload,
    }

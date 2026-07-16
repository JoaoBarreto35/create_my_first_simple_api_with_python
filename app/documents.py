"""Download seguro e extração de conteúdo de anexos.

A API central extrai/transcreve o conteúdo, mas não decide se a autorização é
válida. A decisão auditável continua no ERP.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import mimetypes
from pathlib import PurePath
from typing import Any
from urllib.parse import urljoin, urlparse

from docx import Document
from pypdf import PdfReader
import requests

from .errors import IntegrationError
from .fracttal import AttachmentMetadata
from .http import build_session
from .security import host_is_allowed
from .settings import Settings

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_TEXT_MIMES = {"text/plain", "text/csv"}


@dataclass(frozen=True)
class DownloadedAttachment:
    metadata: AttachmentMetadata
    content: bytes
    mime_type: str
    sha256_hex: str


@dataclass(frozen=True)
class ExtractedAttachment:
    id: int
    id_request: int
    description: str
    nome: str
    mime_type: str
    tamanho_bytes: int
    sha256: str
    texto_extraido: str
    metodo_extracao: str
    status_extracao: str
    paginas_analisadas: int
    avisos: tuple[str, ...]

    def to_erp_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "id_request": self.id_request,
            "description": self.description,
            "nome": self.nome,
            "mime_type": self.mime_type,
            "tamanho_bytes": self.tamanho_bytes,
            "sha256": self.sha256,
            "texto_extraido": self.texto_extraido,
            "metodo_extracao": self.metodo_extracao,
            "status_extracao": self.status_extracao,
            "paginas_analisadas": self.paginas_analisadas,
            "avisos": list(self.avisos),
        }


def _safe_filename(name: str, attachment_id: int) -> str:
    clean = PurePath(name or "").name.replace("\x00", "").strip()
    return (clean or f"anexo-{attachment_id}")[:255]


def _sniff_mime(content: bytes, header_mime: str, filename: str) -> str:
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"PK\x03\x04") and filename.lower().endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    normalized_header = (header_mime or "").split(";", 1)[0].strip().lower()
    if normalized_header in _IMAGE_MIMES | _TEXT_MIMES | {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        return normalized_header
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or normalized_header or "application/octet-stream"


def _validate_signed_url(url: str, settings: Settings) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not host_is_allowed(
        parsed.hostname, settings.allowed_attachment_hosts
    ):
        raise IntegrationError(
            "attachment_url_not_allowed",
            "A URL assinada do anexo não pertence a um host permitido.",
            status_code=422,
        )


def download_attachment(
    metadata: AttachmentMetadata,
    settings: Settings,
) -> DownloadedAttachment:
    if not metadata.signed_url:
        raise IntegrationError(
            "attachment_without_url",
            "O Fracttal retornou um anexo sem URL de download.",
            status_code=422,
        )
    current_url = metadata.signed_url
    session = build_session(settings)
    response: requests.Response | None = None
    try:
        for _ in range(4):
            _validate_signed_url(current_url, settings)
            try:
                response = session.get(
                    current_url,
                    stream=True,
                    allow_redirects=False,
                    headers={"Accept": "*/*"},
                    timeout=(settings.connect_timeout, settings.read_timeout),
                )
            except requests.Timeout as exc:
                raise IntegrationError(
                    "attachment_download_timeout",
                    "O download do anexo não respondeu a tempo.",
                ) from exc
            except requests.RequestException as exc:
                raise IntegrationError(
                    "attachment_download_error",
                    "Não foi possível baixar o anexo do Fracttal.",
                ) from exc

            if response.status_code in _REDIRECT_CODES:
                location = response.headers.get("Location")
                if not location:
                    raise IntegrationError(
                        "attachment_invalid_redirect",
                        "O download do anexo retornou redirecionamento inválido.",
                    )
                current_url = urljoin(current_url, location)
                response.close()
                response = None
                continue
            break
        else:
            raise IntegrationError(
                "attachment_too_many_redirects",
                "O download do anexo excedeu o limite de redirecionamentos.",
            )

        if response is None:
            raise IntegrationError("attachment_download_error", "Resposta de download ausente.")
        if response.status_code in (401, 403):
            raise IntegrationError(
                "attachment_signed_url_expired",
                "A URL assinada do anexo expirou ou foi recusada.",
                upstream_status=response.status_code,
            )
        if response.status_code >= 400:
            raise IntegrationError(
                "attachment_download_upstream_error",
                "O armazenamento do anexo retornou erro.",
                upstream_status=response.status_code,
            )

        final_host = urlparse(response.url or current_url).hostname
        if not host_is_allowed(final_host, settings.allowed_attachment_hosts):
            raise IntegrationError(
                "attachment_redirect_host_not_allowed",
                "O download foi redirecionado para um host não permitido.",
            )

        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                if int(raw_length) > settings.max_attachment_bytes:
                    raise IntegrationError(
                        "attachment_too_large",
                        "O anexo excede o limite de tamanho configurado.",
                        status_code=413,
                    )
            except ValueError:
                pass

        chunks: list[bytes] = []
        size = 0
        digest = sha256()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > settings.max_attachment_bytes:
                raise IntegrationError(
                    "attachment_too_large",
                    "O anexo excede o limite de tamanho configurado.",
                    status_code=413,
                )
            digest.update(chunk)
            chunks.append(chunk)
        content = b"".join(chunks)
        filename = _safe_filename(metadata.description, metadata.id)
        mime_type = _sniff_mime(
            content,
            response.headers.get("Content-Type", ""),
            filename,
        )
        return DownloadedAttachment(
            metadata=metadata,
            content=content,
            mime_type=mime_type,
            sha256_hex=digest.hexdigest(),
        )
    finally:
        if response is not None:
            response.close()
        session.close()


def _extract_pdf_text(content: bytes, max_pages: int) -> tuple[str, int, list[str]]:
    warnings: list[str] = []
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return "", 0, ["PDF protegido por senha ou criptografia não suportada"]
        page_count = min(len(reader.pages), max_pages)
        parts: list[str] = []
        for index in range(page_count):
            try:
                text = reader.pages[index].extract_text() or ""
            except Exception:
                text = ""
                warnings.append(f"Não foi possível extrair diretamente a página {index + 1}")
            if text.strip():
                parts.append(text.strip())
        if len(reader.pages) > max_pages:
            warnings.append(
                f"PDF limitado às primeiras {max_pages} páginas para processamento"
            )
        return "\n\n".join(parts).strip(), page_count, warnings
    except Exception as exc:
        return "", 0, [f"PDF inválido ou não processável: {type(exc).__name__}"]


def _extract_docx_text(content: bytes) -> tuple[str, list[str]]:
    try:
        document = Document(BytesIO(content))
        lines = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
        for table in document.tables:
            for row in table.rows:
                values = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if values:
                    lines.append(" | ".join(values))
        return "\n".join(lines).strip(), []
    except Exception as exc:
        return "", [f"DOCX inválido ou não processável: {type(exc).__name__}"]


def _transcribe_with_gemini(content: bytes, mime_type: str, settings: Settings) -> str:
    if not settings.ocr_with_gemini or not settings.document_gemini_api_key:
        return ""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.document_gemini_api_key)
        prompt = (
            "Atue somente como transcritor OCR. Transcreva fielmente todo o texto "
            "visível no documento, preservando nomes, e-mails, datas, cargos, status, "
            "números de requisição, frases de autorização e locais. Não decida se o "
            "documento é válido, não resuma e não complete informações ausentes. "
            "Retorne apenas o texto transcrito."
        )
        response = client.models.generate_content(
            model=settings.document_gemini_model,
            contents=[
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(data=content, mime_type=mime_type),
            ],
            config=types.GenerateContentConfig(temperature=0.0),
        )
        return str(getattr(response, "text", "") or "").strip()
    except Exception:
        # O erro detalhado não é exposto para evitar vazamento de informações da
        # integração. O ERP receberá o anexo como pendente de validação manual.
        return ""


def extract_attachment(downloaded: DownloadedAttachment, settings: Settings) -> ExtractedAttachment:
    metadata = downloaded.metadata
    filename = _safe_filename(metadata.description, metadata.id)
    mime_type = downloaded.mime_type
    text = ""
    method = "none"
    pages = 0
    warnings: list[str] = []

    if mime_type == "application/pdf":
        text, pages, pdf_warnings = _extract_pdf_text(
            downloaded.content, settings.max_pdf_pages
        )
        warnings.extend(pdf_warnings)
        method = "pdf_text" if text else "none"
        if len(text.strip()) < 40:
            ocr_text = _transcribe_with_gemini(
                downloaded.content, "application/pdf", settings
            )
            if ocr_text:
                text = ocr_text
                method = "gemini_ocr_pdf"
    elif mime_type in _IMAGE_MIMES:
        text = _transcribe_with_gemini(downloaded.content, mime_type, settings)
        method = "gemini_ocr_image" if text else "none"
    elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        text, docx_warnings = _extract_docx_text(downloaded.content)
        warnings.extend(docx_warnings)
        method = "docx_text" if text else "none"
    elif mime_type in _TEXT_MIMES:
        try:
            text = downloaded.content.decode("utf-8-sig", errors="replace").strip()
        except Exception:
            text = ""
        method = "plain_text" if text else "none"
    else:
        warnings.append(f"Formato não suportado para extração automática: {mime_type}")

    if text:
        status = "EXTRAIDO"
    elif mime_type in _IMAGE_MIMES or mime_type == "application/pdf":
        status = "SEM_TEXTO_LEGIVEL"
        if not settings.ocr_with_gemini:
            warnings.append("OCR por IA não está habilitado na API central")
        elif not settings.document_gemini_api_key:
            warnings.append("Chave de OCR por IA não configurada")
        else:
            warnings.append("OCR não conseguiu obter texto legível")
    else:
        status = "VALIDACAO_MANUAL"

    return ExtractedAttachment(
        id=metadata.id,
        id_request=metadata.id_request,
        description=metadata.description,
        nome=filename,
        mime_type=mime_type,
        tamanho_bytes=len(downloaded.content),
        sha256=downloaded.sha256_hex,
        texto_extraido=text[:200_000],
        metodo_extracao=method,
        status_extracao=status,
        paginas_analisadas=pages,
        avisos=tuple(warnings),
    )



def render_attachment_preview(downloaded: DownloadedAttachment) -> tuple[bytes, str]:
    """Retorna uma imagem exibível pelo frontend para fotos e PDFs.

    Imagens são preservadas sem recodificação. Para PDFs, somente a primeira
    página é renderizada em PNG, suficiente para a conferência visual solicitada
    pelo ERP sem alterar o conteúdo usado na extração documental.
    """
    if downloaded.mime_type in _IMAGE_MIMES:
        return downloaded.content, downloaded.mime_type
    if downloaded.mime_type == "application/pdf":
        try:
            import pymupdf

            document = pymupdf.open(stream=downloaded.content, filetype="pdf")
            try:
                if document.page_count < 1:
                    raise ValueError("PDF sem páginas")
                page = document.load_page(0)
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), alpha=False)
                return pixmap.tobytes("png"), "image/png"
            finally:
                document.close()
        except Exception as exc:
            raise IntegrationError(
                "attachment_preview_unavailable",
                "Não foi possível gerar a visualização da primeira página do PDF.",
                status_code=422,
            ) from exc
    raise IntegrationError(
        "attachment_preview_unsupported",
        "O formato deste anexo não possui visualização de imagem disponível.",
        status_code=415,
    )

def process_attachment(metadata: AttachmentMetadata, settings: Settings) -> ExtractedAttachment:
    try:
        downloaded = download_attachment(metadata, settings)
        return extract_attachment(downloaded, settings)
    except IntegrationError as exc:
        return ExtractedAttachment(
            id=metadata.id,
            id_request=metadata.id_request,
            description=metadata.description,
            nome=_safe_filename(metadata.description, metadata.id),
            mime_type="application/octet-stream",
            tamanho_bytes=0,
            sha256="",
            texto_extraido="",
            metodo_extracao="none",
            status_extracao="ERRO_INTEGRACAO",
            paginas_analisadas=0,
            avisos=(f"{exc.error_type}: {exc.message}",),
        )


def process_attachments(
    attachments: list[AttachmentMetadata], settings: Settings
) -> list[ExtractedAttachment]:
    if not attachments:
        return []
    workers = min(settings.attachment_workers, len(attachments))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fracttal-anexo") as pool:
        return list(pool.map(lambda item: process_attachment(item, settings), attachments))

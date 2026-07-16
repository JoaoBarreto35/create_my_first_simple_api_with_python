from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import pytest

import main
from app.documents import DownloadedAttachment, extract_attachment
from app.errors import IntegrationError
from app.fracttal import AttachmentMetadata, FracttalClient
from app.settings import load_settings


@pytest.fixture()
def configured_settings(monkeypatch):
    base = load_settings()
    configured = replace(
        base,
        api_secret_token="test-secret",
        fracttal_basic_key="key",
        fracttal_basic_secret="secret",
        document_gemini_api_key=None,
        ocr_with_gemini=False,
    )
    monkeypatch.setattr(main, "settings", configured)
    return configured


@pytest.fixture()
def client(configured_settings):
    return TestClient(main.app)


def auth_headers():
    return {"Authorization": "Bearer test-secret"}


def test_health_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_private_endpoint_requires_token(client):
    response = client.get("/check_health")
    assert response.status_code == 401


def test_bridge_rejects_external_url(client):
    response = client.get(
        "/api/bridge",
        headers=auth_headers(),
        params={"url": "https://example.com/api/items", "token": "abc"},
    )
    assert response.status_code == 400


def test_metadata_endpoint_contract(client, monkeypatch):
    items = [
        AttachmentMetadata(1, 11, "autorizacao.pdf", "https://fracttal-fs.s3.amazonaws.com/a"),
        AttachmentMetadata(2, 11, "email.jpg", "https://fracttal-fs.s3.amazonaws.com/b"),
    ]

    def fake_list(self, code, **kwargs):
        assert code == "11"
        return items, 2

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fake_list)
    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["total"] == 2
    assert "signed_path_image" not in body["data"][0]
    assert body["data"][0]["download_disponivel"] is True


def test_processed_endpoint_returns_erp_key(client, monkeypatch):
    item = AttachmentMetadata(1, 11, "autorizacao.txt", "https://fracttal-fs.s3.amazonaws.com/a")

    def fake_list(self, code, **kwargs):
        return [item], 1

    class FakeExtracted:
        id = 1
        mime_type = "text/plain"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 1,
                "id_request": 11,
                "description": "autorizacao.txt",
                "texto_extraido": "De acordo",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fake_list)
    monkeypatch.setattr(main, "process_attachments", lambda attachments, settings: [FakeExtracted()])
    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["processing_status"] == "CONCLUIDO"
    assert body["anexos_autorizacao"][0]["texto_extraido"] == "De acordo"


def test_no_attachments_is_success_not_integration_error(client, monkeypatch):
    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([], 0),
    )
    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["processing_status"] == "SEM_ANEXOS"
    assert body["anexos_autorizacao"] == []


def test_integration_error_is_structured(client, monkeypatch):
    def fail(self, code, **kwargs):
        raise IntegrationError(
            "fracttal_authentication_error",
            "Credenciais recusadas.",
            upstream_status=401,
        )

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fail)
    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos",
        headers=auth_headers(),
    )
    assert response.status_code == 502
    body = response.json()
    assert body["success"] is False
    assert body["error_type"] == "fracttal_authentication_error"
    assert body["data"] == []


def test_plain_text_extraction(configured_settings):
    metadata = AttachmentMetadata(1, 11, "autorizacao.txt", "https://example.invalid")
    content = "Estou ciente e de acordo para cópia de chaves.".encode("utf-8")
    downloaded = DownloadedAttachment(
        metadata=metadata,
        content=content,
        mime_type="text/plain",
        sha256_hex="abc",
    )
    extracted = extract_attachment(downloaded, configured_settings)
    assert extracted.status_extracao == "EXTRAIDO"
    assert "de acordo" in extracted.texto_extraido
    assert extracted.metodo_extracao == "plain_text"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_fracttal_pagination(configured_settings):
    first = {
        "success": True,
        "data": [
            {
                "id": 1,
                "id_request": 11,
                "description": "a.jpg",
                "signed_path_image": "https://fracttal-fs.s3.amazonaws.com/a",
            }
        ],
        "total": 2,
    }
    second = {
        "success": True,
        "data": [
            {
                "id": 2,
                "id_request": 11,
                "description": "b.pdf",
                "signed_path_image": "https://fracttal-fs.s3.amazonaws.com/b",
            }
        ],
        "total": 2,
    }
    session = FakeSession([FakeResponse(first), FakeResponse(second)])
    client = FracttalClient(configured_settings, session=session)
    attachments, total = client.list_request_attachments("11", limit=1)
    assert total == 2
    assert [item.id for item in attachments] == [1, 2]
    assert session.calls[1][1]["params"]["start"] == 1


def test_invalid_request_code(configured_settings):
    client = FracttalClient(configured_settings, session=FakeSession([]))
    with pytest.raises(IntegrationError) as exc:
        client.list_request_attachments("../../etc/passwd")
    assert exc.value.error_type == "invalid_request_code"



def test_processed_image_contains_signed_preview_path(client, monkeypatch):
    item = AttachmentMetadata(
        8,
        11,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.jpg",
    )

    class FakeExtracted:
        id = 8
        mime_type = "image/jpeg"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 8,
                "id_request": 11,
                "description": "autorizacao.jpg",
                "mime_type": "image/jpeg",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    monkeypatch.setattr(main, "process_attachments", lambda attachments, settings: [FakeExtracted()])

    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    preview = response.json()["anexos_autorizacao"][0]["imagem_analisada"]
    parsed = urlparse(preview)
    assert parsed.path == "/api/fracttal/solicitacoes/11/anexos/8/visualizacao"
    query = parse_qs(parsed.query)
    assert int(query["expires"][0]) > 0
    assert len(query["signature"][0]) == 64


def test_signed_preview_endpoint_returns_image(client, monkeypatch):
    item = AttachmentMetadata(
        8,
        11,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.jpg",
    )
    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    downloaded = DownloadedAttachment(
        metadata=item,
        content=b"fake-image",
        mime_type="image/jpeg",
        sha256_hex="abc",
    )
    class FakeExtracted:
        id = 8
        mime_type = "image/jpeg"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 8,
                "id_request": 11,
                "description": "autorizacao.jpg",
                "mime_type": "image/jpeg",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(main, "process_attachments", lambda attachments, settings: [FakeExtracted()])
    monkeypatch.setattr(main, "download_attachment", lambda metadata, settings: downloaded)
    monkeypatch.setattr(main, "render_attachment_preview", lambda value: (b"preview", "image/jpeg"))

    processed = client.get(
        "/api/fracttal/solicitacoes/11/anexos/processados",
        headers=auth_headers(),
    )
    preview = processed.json()["anexos_autorizacao"][0]["imagem_analisada"]
    response = client.get(preview)
    assert response.status_code == 200
    assert response.content == b"preview"
    assert response.headers["content-type"].startswith("image/jpeg")


def test_attachment_endpoint_falls_back_to_query_parameter(configured_settings):
    not_found = FakeResponse({}, status_code=404)
    found = FakeResponse({
        "success": True,
        "data": [{
            "id": 9,
            "id_request": 11,
            "description": "autorizacao.jpg",
            "signed_path_image": "https://fracttal-fs.s3.amazonaws.com/a",
        }],
        "total": 1,
    })
    session = FakeSession([not_found, found])
    client = FracttalClient(configured_settings, session=session)
    attachments, total = client.list_request_attachments("11")
    assert total == 1
    assert attachments[0].id == 9
    assert session.calls[1][1]["params"]["id_request"] == "11"


def test_legacy_fracttal_credentials_are_available_without_render_env(monkeypatch):
    for name in (
        "FRACTTAL_BASIC_TOKEN",
        "FRACTTAL_BASIC_KEY",
        "FRACTTAL_BASIC_SECRET",
        "FRACTTAL_KEY",
        "FRACTTAL_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    loaded = load_settings()
    assert loaded.fracttal_basic_key
    assert loaded.fracttal_basic_secret


def test_processed_image_contains_separate_original_download_path(client, monkeypatch):
    item = AttachmentMetadata(
        18,
        51329,
        "autorizacao.pdf",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.pdf",
    )

    class FakeExtracted:
        id = 18
        mime_type = "application/pdf"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 18,
                "id_request": 51329,
                "description": "autorizacao.pdf",
                "nome": "autorizacao.pdf",
                "mime_type": "application/pdf",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    monkeypatch.setattr(
        main,
        "process_attachments",
        lambda attachments, settings: [FakeExtracted()],
    )

    response = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    data = response.json()["anexos_autorizacao"][0]

    preview = urlparse(data["imagem_analisada"])
    download = urlparse(data["arquivo_original_url"])
    assert preview.path.endswith("/visualizacao")
    assert download.path.endswith("/download")
    assert data["arquivo_url"] == data["imagem_analisada"]
    assert parse_qs(preview.query)["signature"][0] != parse_qs(download.query)["signature"][0]


def test_signed_download_endpoint_returns_original_file_as_attachment(client, monkeypatch):
    item = AttachmentMetadata(
        18,
        51329,
        "Autorização chaveiro.pdf",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.pdf",
    )

    class FakeExtracted:
        id = 18
        mime_type = "application/pdf"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 18,
                "id_request": 51329,
                "description": "Autorização chaveiro.pdf",
                "nome": "Autorização chaveiro.pdf",
                "mime_type": "application/pdf",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    monkeypatch.setattr(
        main,
        "process_attachments",
        lambda attachments, settings: [FakeExtracted()],
    )
    monkeypatch.setattr(
        main,
        "download_attachment",
        lambda metadata, settings: DownloadedAttachment(
            metadata=metadata,
            content=b"%PDF-original-content",
            mime_type="application/pdf",
            sha256_hex="abc",
        ),
    )

    processed = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers=auth_headers(),
    )
    download_url = processed.json()["anexos_autorizacao"][0]["arquivo_original_url"]
    response = client.get(download_url)

    assert response.status_code == 200
    assert response.content == b"%PDF-original-content"
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


def test_preview_signature_cannot_be_reused_for_download(client, monkeypatch):
    item = AttachmentMetadata(
        18,
        51329,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.jpg",
    )

    class FakeExtracted:
        id = 18
        mime_type = "image/jpeg"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 18,
                "id_request": 51329,
                "description": "autorizacao.jpg",
                "mime_type": "image/jpeg",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    monkeypatch.setattr(
        main,
        "process_attachments",
        lambda attachments, settings: [FakeExtracted()],
    )

    processed = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers=auth_headers(),
    )
    preview = urlparse(processed.json()["anexos_autorizacao"][0]["imagem_analisada"])
    forged_download = preview._replace(path=preview.path.replace("/visualizacao", "/download")).geturl()

    response = client.get(forged_download)
    assert response.status_code == 403
    assert response.json()["error_type"] == "attachment_access_invalid_signature"

from __future__ import annotations

from dataclasses import replace
from io import BytesIO

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

# ERP Manutenção — API Central 2.0

API FastAPI usada pelo ERP para acessar o Fracttal e o Gemini sem executar essas
requisições diretamente no aplicativo desktop.

## Compatibilidade preservada

Os endpoints já usados pelo ERP continuam disponíveis com o mesmo contrato:

- `GET /api/bridge`
- `GET /api/executar`
- `GET /check_health`

O bridge legado agora aceita somente URLs HTTPS da API oficial do Fracttal. Isso
reduz o risco de SSRF sem alterar as consultas atuais do ERP.

## Nova integração de anexos

### Consultar metadados

```http
GET /api/fracttal/solicitacoes/{code}/anexos
Authorization: Bearer <API_SECRET_TOKEN>
```

Parâmetros opcionais:

- `start` (padrão `0`)
- `limit` (máximo `100`)
- `paginate_all` (padrão `true`)
- `include_signed_url` (padrão `false`)

A paginação é executada automaticamente e limitada pelas configurações de
segurança da API.

### Processar documentos para o ERP_22

```http
GET /api/fracttal/solicitacoes/{code}/anexos/processados
Authorization: Bearer <API_SECRET_TOKEN>
```

Esse endpoint:

1. consulta todos os anexos da solicitação;
2. baixa cada URL assinada imediatamente;
3. valida HTTPS, host, redirecionamentos, tamanho e tipo real do arquivo;
4. extrai texto de PDF textual, DOCX e TXT;
5. usa Gemini somente como OCR/transcrição de imagens ou PDF escaneado, quando
   essa opção estiver configurada;
6. devolve a chave `anexos_autorizacao`, já compatível com a estrutura preparada
   no ERP_22;
7. inclui uma URL temporária assinada em `imagem_analisada` para fotos e para a
   primeira página de PDFs, permitindo a exibição pelo botão de chave do ERP.

A API **não decide** se a autorização é válida. A decisão permanece no módulo
determinístico `document_authorization.py` do ERP.

Exemplo resumido:

```json
{
  "success": true,
  "code": "55365",
  "processing_status": "CONCLUIDO",
  "anexos_autorizacao": [
    {
      "id": 3,
      "id_request": 55365,
      "description": "de acordo.pdf",
      "mime_type": "application/pdf",
      "texto_extraido": "Estou ciente e de acordo...",
      "metodo_extracao": "pdf_text",
      "status_extracao": "EXTRAIDO"
    }
  ]
}
```

### Sem anexo versus erro de integração

- Consulta bem-sucedida sem arquivos: HTTP `200`, `processing_status=SEM_ANEXOS`
  e `anexos_autorizacao=[]`.
- Erro de autenticação, rede ou payload: resposta HTTP de erro com
  `success=false` e `error_type` específico.

Isso impede que uma indisponibilidade do Fracttal seja interpretada como ausência
de autorização.

## Variáveis obrigatórias no Render

```text
API_SECRET_TOKEN
FRACTTAL_BASIC_KEY
FRACTTAL_BASIC_SECRET
```

Para OCR de prints e PDFs escaneados:

```text
DOCUMENT_GEMINI_API_KEY
DOCUMENT_OCR_WITH_GEMINI=true
```

A IA é usada apenas para transcrever o conteúdo visível. A validação da
aprovação continua determinística no ERP.

Use `.env.example` como referência completa.

## Implantação no Render

O `render.yaml` já contém:

```text
Build: pip install -r requirements.txt
Start: uvicorn main:app --host 0.0.0.0 --port $PORT --no-access-log
Health: /health
```

O acesso log foi desativado porque os endpoints legados ainda recebem
credenciais como parâmetros de URL. Os novos endpoints especializados usam as
credenciais armazenadas exclusivamente no ambiente do Render.

## Desenvolvimento e testes

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
uvicorn main:app --reload
```

## Próximo encaixe no ERP

Ao consumir o endpoint processado, o ERP deve fazer:

```python
chamado["anexos_autorizacao"] = resposta["anexos_autorizacao"]
```

Se a API central retornar erro HTTP, deve preencher:

```python
chamado["validacao_documental"] = {
    "aplicavel": True,
    "status": "ERRO_INTEGRACAO",
    "resumo": "Não foi possível consultar ou analisar os anexos.",
    "bloqueia_conversao": True,
}
```

Essa ligação é a única etapa restante para ativar a consulta real no ERP.


## Visualização e download do anexo

Para imagens e PDFs processados, cada item retorna:

- `imagem_analisada`: URL temporária para visualização rápida no ERP;
- `arquivo_url`: alias legado da mesma prévia, preservado por compatibilidade;
- `arquivo_original_url`: URL temporária exclusiva para download do arquivo original.

A rota de visualização responde com `Content-Disposition: inline`. A rota do
arquivo original responde com `Content-Disposition: attachment`, portanto não
há download automático ao abrir a prévia.

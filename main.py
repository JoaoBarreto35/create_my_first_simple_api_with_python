from fastapi import FastAPI, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import os
from google import genai
from google.genai import types
import requests

app = FastAPI()
security = HTTPBearer()

# O token real será lido de uma variável de ambiente (definida no Render)
API_SECRET_TOKEN = os.getenv("API_SECRET_TOKEN")

def minha_funcao_original(api_key,modelo,CONTEXTO_CLASSIFICACAO,texto):
    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        return False, f"❌ Erro ao criar client: {e}"
        
    try:
        chat = client.chats.create(
        model=modelo,
        config=types.GenerateContentConfig(
        system_instruction=CONTEXTO_CLASSIFICACAO,
        temperature=0.0,
        response_mime_type="application/json"
        )
        )

        # atualizar_ui("📡 Enviando requisição...")
        resposta = chat.send_message(texto)

        # atualizar_ui("✅ Resposta recebida")

        return True, resposta.text

    except Exception as e:

        return False, e
    
def bridge(url, token):
    headers = {"Authorization": f"Basic {token}"}

    try:
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            data = response.json().get("data", [])
            return data
        elif response.status_code in [401,403]:
            return []
        else:
            return []
    except:
        return []
    



@app.get("/api/executar")
def executar_funcao(api_key,modelo,CONTEXTO_CLASSIFICACAO,texto, credentials: HTTPAuthorizationCredentials = Security(security)):
    # Compara o token enviado pelo usuário com o token configurado
    if credentials.credentials != API_SECRET_TOKEN:
        raise HTTPException(
            status_code=401, 
            detail="Token inválido ou não fornecido"
        )
    
    resultado = minha_funcao_original(api_key,modelo,CONTEXTO_CLASSIFICACAO,texto)
    return {"resultado": resultado}

@app.get("/api/bridge")
def executar_bridge(url,token, credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != API_SECRET_TOKEN:
        raise HTTPException(
            status_code=401, 
            detail="Token inválido ou não fornecido"
        )
    
    resultado = bridge(url,token)
    return {"resultado": resultado}

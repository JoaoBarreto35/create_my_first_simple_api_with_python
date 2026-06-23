from fastapi import FastAPI, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import os

app = FastAPI()
security = HTTPBearer()

# O token real será lido de uma variável de ambiente (definida no Render)
API_SECRET_TOKEN = os.getenv("API_SECRET_TOKEN", "senha_padrao_para_testes")

def minha_funcao_original(nome: str):
    return f"Olá, {nome}! A API está funcionando."

@app.get("/api/executar")
def executar_funcao(nome: str, credentials: HTTPAuthorizationCredentials = Security(security)):
    # Compara o token enviado pelo usuário com o token configurado
    if credentials.credentials != API_SECRET_TOKEN:
        raise HTTPException(
            status_code=401, 
            detail="Token inválido ou não fornecido"
        )
    
    resultado = minha_funcao_original(nome)
    return {"resultado": resultado}

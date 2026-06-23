from fastapi import FastAPI

app = FastAPI()

# Sua função existente
def minha_funcao_original(nome: str):
    return f"Olá, {nome}! A API está funcionando."

# Rota da sua API
@app.get("/api/executar")
def executar_funcao(nome: str):
    resultado = minha_funcao_original(nome)
    return {"resultado": resultado}

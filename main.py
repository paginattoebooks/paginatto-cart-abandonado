from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Paginatto - Carrinho Abandonado", version="0.1.0")

@app.get("/health")
def health():
    return {"ok": True, "service": "paginatto", "version": "0.1.0"}

# Endpoint que o CartPanda vai chamar por webhook
@app.post("/webhook/cartpanda")
async def cartpanda_webhook(request: Request):
    payload = await request.json()
    # por enquanto só devolve o que recebeu (eco).
    return JSONResponse({"received": True, "echo": payload})

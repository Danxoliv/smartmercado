"""Proteção por PIN (variável APP_PIN) para todas as rotas /api."""

import hmac
import os
import threading
import time

from fastapi import Header, HTTPException, Request

MAX_FALHAS = 5
BLOQUEIO_SEG = 300

# Contagem de falhas por IP. Em serverless é por instância (melhor esforço), mas atrasa força bruta.
_falhas: dict[str, tuple[int, float]] = {}
_lock = threading.Lock()


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def exigir_pin(request: Request, x_app_pin: str | None = Header(default=None)) -> None:
    pin = os.environ.get("APP_PIN", "")
    if not pin:
        raise HTTPException(503, "APP_PIN não configurado no servidor.")
    ip = _ip(request)
    agora = time.monotonic()
    with _lock:
        falhas, ate = _falhas.get(ip, (0, 0.0))
        if falhas >= MAX_FALHAS and agora < ate:
            raise HTTPException(429, "Muitas tentativas. Aguarde alguns minutos.")
    if x_app_pin and hmac.compare_digest(x_app_pin.encode(), pin.encode()):
        with _lock:
            _falhas.pop(ip, None)
        return
    with _lock:
        falhas, ate = _falhas.get(ip, (0, 0.0))
        if agora >= ate and falhas >= MAX_FALHAS:
            falhas = 0
        _falhas[ip] = (falhas + 1, agora + BLOQUEIO_SEG)
    raise HTTPException(401, "PIN inválido.")


def _resetar():  # usado nos testes
    with _lock:
        _falhas.clear()

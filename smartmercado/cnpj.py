"""CNPJ -> tipo de estabelecimento, via BrasilAPI (GET /api/cnpj/v1/{cnpj}: cnae_fiscal, cnae_fiscal_descricao).

Nunca bloqueia o fluxo: qualquer falha (rede, timeout, resposta estranha, cache) devolve None.
O host é fixo (não vem de dado externo), o CNPJ vira só dígitos no caminho, não segue redirecionamento
e a resposta é limitada em tamanho. O resultado fica em cache no Firestore (coleção `cnpjs`, id = CNPJ).
"""

import json
import re
from datetime import datetime

import httpx

from .util import TZ, so_digitos

HOST = "brasilapi.com.br"
COLECAO = "cnpjs"
MAX_BYTES = 1024 * 1024
TIMEOUT = httpx.Timeout(4.0, connect=3.0)

# Prefixo do CNAE (2 a 7 dígitos) -> tipo. Vale o prefixo mais longo que casar.
PREFIXOS = {
    "4721102": "padaria", "1091102": "padaria",
    **dict.fromkeys(("4711", "4712", "4721", "4722", "4723", "4724", "4729"), "mercado"),
    "4771": "farmacia",
    "4731": "posto", "4732": "posto",
    **dict.fromkeys(("5611", "5612", "5620"), "restaurante"),
    **dict.fromkeys(("4741", "4742", "4743", "4744", "4751", "4752", "4753", "4754", "4755", "4756", "4757", "4759"), "casa_utilidades"),
    **dict.fromkeys(("4781", "4782", "4783"), "vestuario"),
    "85": "educacao",
    **dict.fromkeys(("49", "50", "51"), "transporte"),
    **dict.fromkeys(("59", "90", "91", "93"), "lazer"),
    **dict.fromkeys(("61", "62", "63", "69", "74", "75", "77", "95", "96"), "servicos"),
}


def tipo_por_cnae(cnae) -> str | None:
    s = re.sub(r"\D", "", str(cnae or ""))
    if not s:
        return None
    s = s.zfill(7)[:7]  # int perde o zero à esquerda (ex.: 0111301)
    for n in range(7, 1, -1):
        if s[:n] in PREFIXOS:
            return PREFIXOS[s[:n]]
    return None


def consultar_brasilapi(cnpj: str, client: httpx.Client | None = None) -> dict | None:
    d = so_digitos(cnpj)
    if len(d) != 14:
        return None
    proprio = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers={"Accept": "application/json"})
    try:
        with client.stream("GET", f"https://{HOST}/api/cnpj/v1/{d}") as resp:
            if resp.status_code != 200:
                return None
            corpo = bytearray()
            for bloco in resp.iter_bytes():
                corpo.extend(bloco)
                if len(corpo) > MAX_BYTES:
                    return None
        dados = json.loads(corpo)
        return {k: dados.get(k) for k in ("cnae_fiscal", "cnae_fiscal_descricao", "razao_social", "nome_fantasia")}
    except Exception:  # falha silenciosa por contrato
        return None
    finally:
        if proprio:
            client.close()


def tipo_do_cnpj(cnpj: str | None, repo, client: httpx.Client | None = None) -> str | None:
    d = so_digitos(cnpj)
    if len(d) != 14:
        return None
    try:
        cache = repo.doc_get(COLECAO, d)
    except Exception:
        cache = None
    if cache:
        return cache.get("tipo")
    dados = consultar_brasilapi(d, client)
    if not dados:
        return None  # sem cache: tenta de novo na próxima nota
    tipo = tipo_por_cnae(dados.get("cnae_fiscal"))
    try:
        repo.doc_set(COLECAO, d, {**dados, "tipo": tipo, "consultado_em": datetime.now(TZ)})
    except Exception:
        pass
    return tipo

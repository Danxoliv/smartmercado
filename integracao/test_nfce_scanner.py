"""Testes do nfce_scanner.py (arquivo único). Rodar:  pip install pytest httpx fastapi python-multipart  &&  pytest integracao"""

import sys
from pathlib import Path

import httpx
import pytest

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import nfce_scanner as ns  # noqa: E402

CHAVE = "52260112345678000195651100000400899000857068"
LINK = f"https://nfeweb.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe?p={CHAVE}|2|1|09|210.63|0123|1|abcd"


def ler(nome: str) -> str:
    return (AQUI / "fixtures" / nome).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def nota():
    return ns.conferir_soma(ns.parse_nfce_html(ns.html_do_render(ler("nfce_go_real_render.xml")), LINK))


def test_nota_real_de_goias(nota):
    assert nota["mercado"] == "SUPERMERCADO EXEMPLO LTDA" and nota["cnpj"] == "12345678000195"
    assert nota["chave"] == CHAVE and nota["data"] == "2026-01-15T10:20:30-03:00"
    assert nota["total"] == 210.63 and nota["soma_itens"] == 210.63 and nota["soma_confere"] is True
    assert len(nota["itens"]) == 29
    assert nota["itens"][0] == {
        "nome": "MANTEIGA CAGEL 200G", "codigo": "7898954365268", "ean": "7898954365268", "qtd": 1.0,
        "unidade": "UN", "preco_unit": 7.99, "preco_total": 7.99, "categoria": "Frios e Laticínios",
    }


def test_valor_a_pagar_com_desconto():
    n = ns.conferir_soma(ns.parse_nfce_html(ler("nfce_go_sintetica.html")))
    assert n["total"] == 50.36 and n["descontos"] == 1.0 and n["soma_confere"] is True


@pytest.mark.parametrize("url", [
    "http://nfeweb.sefaz.go.gov.br/x", "https://nfeweb.sefaz.go.gov.br.evil.com/x", "https://evil.com/?u=nfeweb.sefaz.go.gov.br",
    "https://u:p@nfeweb.sefaz.go.gov.br/x", "https://nfeweb.sefaz.go.gov.br@evil.com/", "https://nfeweb.sefaz.go.gov.br:8443/x",
])
def test_link_recusado(url):
    with pytest.raises(ns.LinkInvalido):
        ns.validar_link(url)


def test_download_em_duas_etapas_com_cookie():
    def handler(req: httpx.Request):
        if req.url.path.endswith("/nfce/danfeNFCe"):
            return httpx.Response(200, text=ler("nfce_go_real_qr.html"), headers={"set-cookie": "JSESSIONID=abc; Path=/nfeweb"})
        if "/render/html/danfeNFCe" in req.url.path:
            if req.headers.get("cookie") != "JSESSIONID=abc":
                return httpx.Response(200, text="sessão expirada")
            return httpx.Response(200, text=ler("nfce_go_real_render.xml"))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert "tabResult" in ns.baixar_pagina(LINK, client)


def test_ssl_ligado(monkeypatch):
    visto = {}

    class Falso(httpx.Client):
        def __init__(self, **kw):
            visto.update(kw)
            super().__init__(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='<table id="tabResult"></table>')))

    monkeypatch.setattr(ns.httpx, "Client", Falso)
    ns.baixar_pagina(LINK)
    assert visto["verify"] is True


@pytest.mark.parametrize("nome,cat", [
    ("REFRIG COCA COLA 2L", "Bebidas"), ("DET YPE NEUTRO 500ML", "Limpeza"), ("PAPEL HIG NEVE FD 12X30M", "Higiene"),
    ("CERV HEINEKEN LN 330ML", "Bebidas Alcoólicas"), ("AGUA SANITARIA QBOA 1L", "Limpeza"),
    ("CREME DE LEITE NESTLE 200G", "Mercearia"), ("DETALHE DECORATIVO", "Outros"), ("REFRIGERADOR PORTATIL", "Outros"),
])
def test_categorias(nome, cat):
    assert ns.categorizar(nome) == cat


def test_chaves():
    assert ns.chave_valida(CHAVE) and not ns.chave_valida("0" * 44) and not ns.chave_valida(CHAVE[:-1] + "0")
    sint = ns.chave_sintetica("MERCADO|2026-10-09|41.48")
    assert len(sint) == 44 and sint.startswith("00") and ns.chave_aceita(sint) and not ns.chave_valida(sint)


def test_foto_descarta_chave_inventada_e_tipo_invalido():
    n = ns.normalizar_resposta({"mercado": "B12", "chave_acesso": "0" * 44, "valor_a_pagar": 5,
                                "itens": [{"nome": "SAL", "quantidade": 1, "preco_unitario": 5, "preco_total": 5}]})
    assert n["chave"] == "" and n["data"] is None
    with pytest.raises(ns.ImagemInvalida):
        ns.ler_foto_bytes(b"%PDF-1.4 ...")


def test_router(monkeypatch):
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    def auth(x_token: str = Header(default="")):
        if x_token != "ok":
            raise HTTPException(401)

    app = FastAPI()
    app.include_router(ns.criar_router([Depends(auth)]), prefix="/api")
    monkeypatch.setattr(ns, "baixar_pagina", lambda url, client=None: ns.html_do_render(ler("nfce_go_real_render.xml")))
    c = TestClient(app)
    assert c.post("/api/nfce/link", json={"url": LINK}).status_code == 401
    r = c.post("/api/nfce/link", json={"url": LINK}, headers={"x-token": "ok"})
    assert r.status_code == 200 and r.json()["origem"] == "qrcode" and len(r.json()["itens"]) == 29
    assert c.post("/api/nfce/link", json={"url": "https://evil.com/x"}, headers={"x-token": "ok"}).status_code == 400
    assert c.post("/api/nfce/foto", files={"foto": ("x.jpg", b"%PDF", "image/jpeg")}, headers={"x-token": "ok"}).status_code == 415

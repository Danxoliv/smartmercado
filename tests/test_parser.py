import httpx
import pytest

from smartmercado import nfce
from smartmercado.models import conferir_soma
from smartmercado.util import chave_valida, gtin_valido, parse_numero_br

from .conftest import CHAVE_FIXTURE

LINK = f"https://nfeweb.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe?p={CHAVE_FIXTURE}|2|1|1|ABCDEF"


# ------------------------------------------------------------- validação do link

@pytest.mark.parametrize("url", [
    LINK,
    "https://NFEWEB.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe?p=1",
    "https://nfeweb.sefaz.go.gov.br:443/nfeweb/x",
])
def test_link_aceito(url):
    assert nfce.validar_link(url) == url


@pytest.mark.parametrize("url", [
    "http://nfeweb.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe?p=1",       # sem HTTPS
    "https://nfeweb.sefaz.go.gov.br.evil.com/nfeweb",                        # sufixo falso
    "https://evil.com/?u=https://nfeweb.sefaz.go.gov.br/",                   # host diferente
    "https://user:senha@nfeweb.sefaz.go.gov.br/nfeweb",                      # credenciais
    "https://nfeweb.sefaz.go.gov.br@evil.com/",                              # truque com @
    "https://nfeweb.sefaz.go.gov.br:8443/nfeweb",                            # porta
    "https://www.sefaz.go.gov.br/nfeweb",                                    # outro subdomínio
    "https://nfe.sefaz.sp.gov.br/x",                                         # outro estado
    "javascript:alert(1)",
    "ftp://nfeweb.sefaz.go.gov.br/x",
    "",
])
def test_link_recusado(url):
    with pytest.raises(nfce.LinkInvalido):
        nfce.validar_link(url)


def test_chave_do_link():
    assert nfce.chave_do_link(LINK) == CHAVE_FIXTURE
    assert nfce.chave_do_link("https://nfeweb.sefaz.go.gov.br/x?p=123") is None


# ------------------------------------------------------------- parser

def test_parse_cabecalho(html_sintetico):
    n = nfce.parse_nfce_html(html_sintetico, LINK)
    assert n["mercado"] == "SUPERMERCADO EXEMPLO LTDA"
    assert n["cnpj"] == "12345678000195"
    assert n["chave"] == CHAVE_FIXTURE
    assert n["data"] == "2026-10-09T13:45:12-03:00"


def test_parse_total_e_valor_a_pagar(html_sintetico):
    n = nfce.parse_nfce_html(html_sintetico)
    assert n["total"] == 50.36          # "Valor a pagar" (com desconto), não o "Valor total"
    assert n["descontos"] == 1.00
    assert isinstance(n["total"], float)


def test_parse_itens(html_sintetico):
    itens = nfce.parse_nfce_html(html_sintetico)["itens"]
    assert len(itens) == 4
    arroz, refri, banana, det = itens
    assert arroz == {
        "nome": "ARROZ TIO JOAO T1 5KG", "codigo": "7893500018322", "ean": "7893500018322",
        "qtd": 1.0, "unidade": "UN", "preco_unit": 27.99, "preco_total": 27.99, "categoria": "Mercearia",
    }
    assert refri["nome"] == "REFRIG. COCA-COLA PET 2L" and refri["categoria"] == "Bebidas"
    assert banana["qtd"] == 1.235 and banana["unidade"] == "KG" and banana["preco_total"] == 7.40
    assert banana["ean"] is None and banana["codigo"] == "2001"
    assert det["qtd"] == 2 and det["unidade"] == "UN" and det["categoria"] == "Limpeza"
    assert det["ean"] is None  # código interno, não é GTIN


def test_soma_confere_considerando_desconto(html_sintetico):
    n = conferir_soma(nfce.parse_nfce_html(html_sintetico))
    assert n["soma_itens"] == 51.36
    assert n["soma_confere"] is True
    assert n["diferenca"] == 0


def test_soma_nao_confere():
    n = conferir_soma({"total": 10.0, "descontos": 0, "itens": [{"preco_total": 7.5}]})
    assert n["soma_confere"] is False and n["diferenca"] == -2.5


def test_chave_pela_url_quando_pagina_nao_tem(html_sintetico):
    html = html_sintetico.replace("5226 1012 3456 7800 0195 6500 1000 0123 4518 7654 3212", "")
    assert nfce.parse_nfce_html(html, LINK)["chave"] == CHAVE_FIXTURE


def test_cnpj_pela_chave_quando_pagina_nao_tem(html_sintetico):
    html = html_sintetico.replace("CNPJ: 12.345.678/0001-95", "")
    assert nfce.parse_nfce_html(html)["cnpj"] == "12345678000195"


def test_pagina_sem_itens():
    with pytest.raises(nfce.NotaNaoReconhecida):
        nfce.parse_nfce_html("<html><body>Nota não encontrada</body></html>")


# ------------------------------------------------------------- download

def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_baixar_pagina_ok(html_sintetico):
    c = _client(lambda req: httpx.Response(200, text=html_sintetico, headers={"content-type": "text/html; charset=utf-8"}))
    assert "SUPERMERCADO EXEMPLO" in nfce.baixar_pagina(LINK, c)


def test_baixar_recusa_redirect_para_outro_host():
    c = _client(lambda req: httpx.Response(302, headers={"location": "https://evil.com/"}))
    with pytest.raises(nfce.LinkInvalido):
        nfce.baixar_pagina(LINK, c)


def test_baixar_segue_redirect_no_mesmo_host(html_sintetico):
    def handler(req):
        if req.url.path == "/nfeweb/sites/nfce/danfeNFCe":
            return httpx.Response(302, headers={"location": "/nfeweb/sites/nfce/consulta"})
        return httpx.Response(200, text=html_sintetico)
    assert "tabResult" in nfce.baixar_pagina(LINK, _client(handler))


def test_baixar_erro_http():
    with pytest.raises(nfce.ErroSefaz):
        nfce.baixar_pagina(LINK, _client(lambda req: httpx.Response(500)))


def test_ssl_ligado_por_padrao(monkeypatch):
    capturado = {}

    class Falso(httpx.Client):
        def __init__(self, **kw):
            capturado.update(kw)
            super().__init__(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<table id=\"tabResult\"></table>")))

    monkeypatch.setattr(nfce.httpx, "Client", Falso)
    nfce.baixar_pagina(LINK)
    assert capturado["verify"] is True


# ------------------------------------------------------------- utilitários

@pytest.mark.parametrize("txt,esperado", [
    ("27,99", 27.99), ("1.234,56", 1234.56), ("0,345", 0.345), ("R$ 10,00", 10.0), ("3", 3.0), ("", None),
])
def test_parse_numero_br(txt, esperado):
    assert parse_numero_br(txt) == esperado


def test_gtin_e_chave():
    assert gtin_valido("7893500018322")
    assert not gtin_valido("7893500018323")
    assert not gtin_valido("12345")
    assert chave_valida(CHAVE_FIXTURE)
    assert not chave_valida(CHAVE_FIXTURE[:-1] + "0")

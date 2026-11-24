"""Layout real da NFC-e de Goiás (baixado da SEFAZ-GO). Loja, CNPJ, chave, endereço e data foram trocados por valores fictícios.

Fixtures: a página do QR Code (só uma casca com iframe) e a resposta XML de
/nfeweb/sites/nfce/render/html/danfeNFCe, que traz o HTML do DANFE escapado.
"""

import httpx
import pytest

from smartmercado import nfce
from smartmercado.models import conferir_soma

from .conftest import FIXTURES

CHAVE = "52260112345678000195651100000400899000857068"
LINK = (
    f"https://nfeweb.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe?p={CHAVE}|2|1|09|210.63|"
    "0123456789abcdef0123456789abcdef|1|0123456789abcdef0123456789abcdef01234567"
)


@pytest.fixture(scope="module")
def pagina_qr() -> str:
    return (FIXTURES / "nfce_go_real_qr.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def render_xml() -> str:
    return (FIXTURES / "nfce_go_real_render.xml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def nota(render_xml) -> dict:
    return conferir_soma(nfce.parse_nfce_html(nfce.html_do_render(render_xml), LINK))


def test_pagina_do_qr_nao_tem_itens(pagina_qr):
    with pytest.raises(nfce.NotaNaoReconhecida):
        nfce.parse_nfce_html(pagina_qr)


def test_cabecalho(nota):
    assert nota["mercado"] == "SUPERMERCADO EXEMPLO LTDA"
    assert nota["cnpj"] == "12345678000195"
    assert nota["chave"] == CHAVE
    assert nota["data"] == "2026-01-15T10:20:30-03:00"


def test_total_e_soma(nota):
    assert nota["total"] == 210.63
    assert nota["descontos"] == 0
    assert nota["soma_itens"] == 210.63 and nota["soma_confere"] is True


def test_itens(nota, render_xml):
    itens = nota["itens"]
    html = nfce.html_do_render(render_xml)
    assert len(itens) == html.count('<tr id="Item + ')
    primeiro = itens[0]
    assert primeiro == {
        "nome": "MANTEIGA CAGEL 200G", "codigo": "7898954365268", "ean": "7898954365268", "qtd": 1.0,
        "unidade": "UN", "preco_unit": 7.99, "preco_total": 7.99, "categoria": "Frios e Laticínios",
    }
    tomate = next(i for i in itens if i["nome"] == "TOMATE KG")
    assert tomate["qtd"] == 0.57 and tomate["unidade"] == "KG" and tomate["preco_unit"] == 9.99
    assert tomate["preco_total"] == 5.69 and tomate["ean"] is None and tomate["codigo"] == "2110"
    assert all(i["nome"] and "Código" not in i["nome"] for i in itens)


@pytest.mark.parametrize("nome,cat", [
    ("BEB DEL VALLE 1LT MARACUJA", "Bebidas"),
    ("BEB ENERGETICO BALY 473ML CEREJA ZERO", "Bebidas"),
    ("MISTURA BOLO FLEISCHMANN 390G CHOCOLATE", "Mercearia"),
    ("BATATINHA KG", "Hortifruti"),
    ("PAPEL HIG CLARA FD 12X1 PREMIUM NEUTRO", "Higiene"),
    ("LAVA ROUPAS TUFF 3LT CONCENTRADO SUBLIME", "Limpeza"),
    ("CREME DE LEITE 200GR ITALAC", "Mercearia"),
    ("LEITE ITAMBE 1,150GR FERMEN TRAD", "Frios e Laticínios"),
    ("PAO DE QUEIJO DA VOVO 700G", "Padaria"),
    ("MOLHO AJINOSHOYU DAV 150ML", "Mercearia"),
])
def test_categorias_da_nota_real(nota, nome, cat):
    assert next(i for i in nota["itens"] if i["nome"] == nome)["categoria"] == cat


def test_fluxo_de_download_da_sefaz_go(pagina_qr, render_xml):
    """QR Code → casca com cookie de sessão → XML do render com o mesmo cookie."""
    pedidos = []

    def handler(req: httpx.Request):
        pedidos.append(req)
        if req.url.path == "/nfeweb/sites/nfce/danfeNFCe":
            return httpx.Response(200, text=pagina_qr, headers={"set-cookie": "JSESSIONID=abc; Path=/nfeweb"})
        if req.url.path == "/nfeweb/sites/nfce/render/html/danfeNFCe":
            if req.headers.get("cookie") != "JSESSIONID=abc":
                return httpx.Response(200, text="<html>sessão expirada</html>")
            return httpx.Response(200, text=render_xml, headers={"content-type": "application/xml;charset=UTF-8"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    html = nfce.baixar_pagina(LINK, client)
    assert [p.url.host for p in pedidos] == ["nfeweb.sefaz.go.gov.br"] * 2
    assert pedidos[1].url.params["chNFe"] == CHAVE
    assert len(nfce.parse_nfce_html(html, LINK)["itens"]) > 0


def test_render_com_erro_da_sefaz():
    xml = "<Map><MESSAGE>Nota nao encontrada</MESSAGE><STATUS>ERROR</STATUS><PARAMS/></Map>"
    with pytest.raises(nfce.ErroSefaz, match="Nota nao encontrada"):
        nfce.html_do_render(xml)


def test_iframe_para_outro_host_e_recusado():
    casca = '<iframe src="https://evil.com/nfeweb/sites/nfce/render/danfeNFCe?chNFe=1"></iframe>'
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=casca)), follow_redirects=False
    )
    with pytest.raises(nfce.LinkInvalido):
        nfce.baixar_pagina("https://nfeweb.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe?p=1", client)

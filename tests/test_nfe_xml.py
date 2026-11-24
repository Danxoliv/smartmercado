"""Fase 1: XML de NF-e (layout nacional), estabelecimento/tipo e prompt generalizado.

Os XMLs em tests/fixtures/nfe_*.xml são SINTÉTICOS (chaves e EANs válidos), não notas reais.
"""

import pytest

from smartmercado import gemini, nfe_xml
from smartmercado.models import conferir_soma

from .conftest import FIXTURES, PIN, gerar_chave

XML = {n: (FIXTURES / f"nfe_{n}.xml").read_bytes() for n in ("mercado", "farmacia", "posto", "utilidades")}


@pytest.mark.parametrize("nome,estab,cnpj,qtd_itens,total,descontos", [
    ("mercado", "SUPERMERCADO TESTE LTDA", "12345678000195", 3, 54.66, 1.00),
    ("farmacia", "DROGARIA EXEMPLO LTDA", "98765432000110", 2, 47.80, 0),
    ("posto", "POSTO EXEMPLO COMBUSTIVEIS LTDA", "11222333000181", 2, 180.69, 0),
    ("utilidades", "CASA E UTILIDADES LTDA", "55666777000199", 3, 91.80, 0),
])
def test_parser_xml(nome, estab, cnpj, qtd_itens, total, descontos):
    n = conferir_soma(nfe_xml.parse_nfe_xml(XML[nome]))
    assert n["estabelecimento"] == n["mercado"] == estab and n["cnpj"] == cnpj
    assert len(n["itens"]) == qtd_itens and n["total"] == total and n["descontos"] == descontos
    assert n["soma_confere"] is True and len(n["chave"]) == 44 and n["data"].endswith("-03:00")


def test_itens_do_xml():
    n = nfe_xml.parse_nfe_xml(XML["mercado"])
    arroz, refri, tomate = n["itens"]
    assert arroz["ean"] == "7893500018322" and arroz["codigo"] == arroz["ean"] and arroz["categoria"] == "Mercearia"
    assert refri["qtd"] == 2.0 and refri["preco_unit"] == 10.99 and refri["preco_total"] == 21.98
    assert tomate["ean"] is None and tomate["codigo"] == "3" and tomate["unidade"] == "KG" and tomate["qtd"] == 0.57
    posto = nfe_xml.parse_nfe_xml(XML["posto"])["itens"][0]
    assert posto["unidade"] == "L" and posto["qtd"] == 30.123 and posto["preco_unit"] == 5.899


def test_nfe_sem_wrapper_e_sem_namespace():
    xml = (
        '<NFe><infNFe Id="NFe' + "5" * 0 + '"><ide><dEmi>2026-10-01</dEmi></ide><emit><xNome>LOJA</xNome></emit>'
        "<det><prod><cProd>1</cProd><xProd>CANETA</xProd><uCom>UN</uCom><qCom>2</qCom><vUnCom>3.50</vUnCom><vProd>7.00</vProd></prod></det>"
        "<total><ICMSTot><vNF>7.00</vNF></ICMSTot></total></infNFe></NFe>"
    ).encode()
    n = nfe_xml.parse_nfe_xml(xml)
    assert n["estabelecimento"] == "LOJA" and n["total"] == 7.0 and n["chave"] == "" and n["data"] == "2026-10-01T00:00:00-03:00"


@pytest.mark.parametrize("xml,msg", [
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><NFe/>', "DOCTYPE"),
    (b'<!DOCTYPE NFe SYSTEM "file:///etc/passwd"><NFe/>', "DOCTYPE"),
    (b'<!ENTITY lol "lol"><NFe/>', "DOCTYPE"),
    (b"<NFe><infNFe>", "válido"),
    (b"<html><body>oi</body></html>", "infNFe"),
    (b'<NFe><infNFe><emit><xNome>X</xNome></emit></infNFe></NFe>', "itens"),
    ("<NFe>é</NFe>".encode("latin-1"), "UTF-8"),
    ("<NFe>é</NFe>".encode("utf-16"), "UTF-8"),
])
def test_xml_recusado(xml, msg):
    with pytest.raises(nfe_xml.XmlInvalido, match=msg):
        nfe_xml.parse_nfe_xml(xml)


def test_rota_xml(client):
    r = client.post("/api/nfce/xml", files={"xml": ("n.xml", XML["farmacia"], "text/xml")})
    assert r.status_code == 200
    d = r.json()
    assert d["origem"] == "xml" and d["estabelecimento"] == d["mercado"] == "DROGARIA EXEMPLO LTDA"
    assert d["tipo_estabelecimento"] is None and d["soma_confere"] is True
    assert client.post("/api/nfce/xml", files={"xml": ("n.xml", b"<DOCTYPE", "text/xml")}).status_code == 422
    grande = b"<a>" + b"x" * nfe_xml.MAX_XML
    assert client.post("/api/nfce/xml", files={"xml": ("n.xml", grande, "text/xml")}).status_code == 413


def test_rota_xml_exige_pin(anon):
    assert anon.post("/api/nfce/xml", files={"xml": ("n.xml", b"<a/>", "text/xml")}).status_code == 401
    assert anon.post("/api/nfce/xml", files={"xml": ("n.xml", b"<a/>", "text/xml")}, headers={"X-App-Pin": PIN}).status_code == 422


def _corpo(**extra):
    base = {"chave": gerar_chave(1), "data": "2026-10-12T09:15:00", "total": 10.0, "origem": "xml",
            "itens": [{"nome": "CANETA", "qtd": 2, "unidade": "UN", "preco_unit": 5.0, "preco_total": 10.0, "categoria": "Outros"}]}
    return {**base, **extra}


def test_salvar_com_estabelecimento_e_legado(client, repo):
    assert client.post("/api/compras", json=_corpo(estabelecimento="Papelaria X", tipo_estabelecimento="casa_utilidades")).status_code == 201
    doc = repo.docs[gerar_chave(1)]
    assert doc["estabelecimento"] == doc["mercado"] == "Papelaria X" and doc["tipo_estabelecimento"] == "casa_utilidades"
    # cliente antigo: só "mercado" -> continua valendo e vira estabelecimento
    chave2 = gerar_chave(2)
    assert client.post("/api/compras", json=_corpo(chave=chave2, mercado="Loja Velha")).status_code == 201
    assert repo.docs[chave2]["estabelecimento"] == "Loja Velha" and repo.docs[chave2]["tipo_estabelecimento"] is None


@pytest.mark.parametrize("extra", [{}, {"estabelecimento": "  "}, {"estabelecimento": "X", "tipo_estabelecimento": "navio"}])
def test_salvar_invalido(client, extra):
    assert client.post("/api/compras", json=_corpo(**extra)).status_code == 422


def test_doc_antigo_sem_estabelecimento_continua_legivel(client, repo):
    """Documento gravado antes da fase 1: só tem `mercado`."""
    from datetime import datetime
    from smartmercado.util import TZ

    repo.docs[gerar_chave(9)] = {"chave": gerar_chave(9), "mercado": "ANTIGO", "cnpj": None, "data": datetime(2026, 10, 3, tzinfo=TZ),
                                 "total": 5.0, "descontos": 0, "origem": "qrcode",
                                 "itens": [{"nome": "SAL", "qtd": 1, "unidade": "UN", "preco_unit": 5.0, "preco_total": 5.0, "categoria": "Mercearia"}]}
    p = client.get("/api/painel?mes=2026-10").json()
    assert p["por_mercado"][0]["mercado"] == "ANTIGO"
    assert client.get("/api/compras?mes=2026-10").json()["compras"][0]["mercado"] == "ANTIGO"


def test_gemini_prompt_geral_e_tipo():
    assert "supermercado, farmácia, posto" in gemini.PROMPT and "estabelecimento" in gemini.SCHEMA["properties"]
    assert "farmacia" in gemini.SCHEMA["properties"]["tipo_estabelecimento"]["enum"]
    ok = {"estabelecimento": "POSTO X", "tipo_estabelecimento": "posto", "valor_a_pagar": 5,
          "itens": [{"nome": "GASOLINA", "quantidade": 1, "preco_unitario": 5, "preco_total": 5}]}
    n = gemini.normalizar_resposta(ok)
    assert n["mercado"] == "POSTO X" and n["tipo_estabelecimento"] == "posto"
    assert gemini.normalizar_resposta({**ok, "tipo_estabelecimento": "inventado"})["tipo_estabelecimento"] is None
    assert gemini.normalizar_resposta({"mercado": "VELHO", "valor_a_pagar": 1, "itens": []})["mercado"] == "VELHO"

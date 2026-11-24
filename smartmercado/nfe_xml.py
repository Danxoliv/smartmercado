"""Leitura do XML da NF-e/NFC-e (layout nacional, qualquer UF) enviado pelo usuário.

Segurança: o XML vem de fora. ElementTree não resolve entidades externas (sem XXE), mas expande entidades
internas; por isso qualquer DOCTYPE/ENTITY é recusado antes de interpretar o arquivo. Tamanho limitado pelo chamador.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime

from .categorias import categorizar
from .util import TZ, chave_valida, cnpj_da_chave, gtin_valido, limpar_espacos, so_digitos

MAX_XML = 2 * 1024 * 1024


class XmlInvalido(ValueError):
    pass


def _t(el: ET.Element | None, caminho: str) -> str:
    """Texto de um descendente, ignorando o namespace (http://www.portalfiscal.inf.br/nfe)."""
    if el is None:
        return ""
    x = el.find("/".join(f"{{*}}{p}" for p in caminho.split("/")))
    return limpar_espacos(x.text or "") if x is not None else ""


def _num(txt: str) -> float | None:
    try:
        return float(txt)
    except ValueError:
        return None


def _data(inf: ET.Element) -> str | None:
    bruto = _t(inf, "ide/dhEmi") or _t(inf, "ide/dEmi")  # dEmi: layouts antigos (só data)
    try:
        dt = datetime.fromisoformat(bruto)
    except ValueError:
        return None
    return (dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)).isoformat()


def parse_nfe_xml(dados: bytes) -> dict:
    """XML (nfeProc ou NFe) -> nota no mesmo formato dos outros leitores. Lança XmlInvalido."""
    try:
        texto = dados.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise XmlInvalido("O XML precisa estar em UTF-8.") from e
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", texto, re.I):
        raise XmlInvalido("XML com DOCTYPE/ENTITY não é aceito.")
    try:
        raiz = ET.fromstring(texto.encode("utf-8"))
    except ET.ParseError as e:
        raise XmlInvalido("Arquivo não é um XML válido.") from e

    inf = raiz if raiz.tag.endswith("}infNFe") or raiz.tag == "infNFe" else raiz.find(".//{*}infNFe")
    if inf is None:
        raise XmlInvalido("Não é uma NF-e/NFC-e (elemento infNFe ausente).")

    chave = so_digitos(inf.get("Id", "")) or so_digitos(_t(raiz, "protNFe/infProt/chNFe"))
    if not chave_valida(chave):
        chave = ""
    cnpj = so_digitos(_t(inf, "emit/CNPJ"))
    if len(cnpj) != 14:
        cnpj = cnpj_da_chave(chave) if chave else ""

    itens = []
    for det in inf.findall("{*}det"):
        prod = det.find("{*}prod")
        nome, total = _t(prod, "xProd"), _num(_t(prod, "vProd"))
        if not nome or total is None:
            continue
        qtd = _num(_t(prod, "qCom")) or 1.0
        unit = _num(_t(prod, "vUnCom"))
        ean = next((c for c in (_t(prod, "cEAN"), _t(prod, "cEANTrib")) if gtin_valido(c)), None)
        itens.append({
            "nome": nome[:200],
            "codigo": ean or _t(prod, "cProd") or None,
            "ean": ean,
            "qtd": qtd,
            "unidade": (_t(prod, "uCom").upper() or "UN")[:10],
            "preco_unit": unit if unit is not None else round(total / qtd, 4),
            "preco_total": round(total, 2),
            "categoria": categorizar(nome),
        })
    if not itens:
        raise XmlInvalido("O XML não tem itens (det/prod).")

    soma = round(sum(i["preco_total"] for i in itens), 2)
    descontos = _num(_t(inf, "total/ICMSTot/vDesc")) or 0.0
    total = _num(_t(inf, "total/ICMSTot/vNF"))
    nome = _t(inf, "emit/xNome") or _t(inf, "emit/xFant")
    return {
        "estabelecimento": nome,
        "mercado": nome,
        "cnpj": cnpj,
        "data": _data(inf),
        "chave": chave,
        "total": round(total, 2) if total is not None else round(soma - descontos, 2),
        "descontos": round(descontos, 2),
        "itens": itens,
    }

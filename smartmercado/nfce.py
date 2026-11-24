"""Leitura da NFC-e de Goiás: validação do link do QR Code, download e parser do HTML."""

import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from .categorias import categorizar
from .util import (
    TZ,
    chave_valida,
    cnpj_da_chave,
    gtin_valido,
    limpar_espacos,
    parse_numero_br,
    so_digitos,
)

HOST_PERMITIDO = "nfeweb.sefaz.go.gov.br"
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Mobile Safari/537.36"
)


class LinkInvalido(ValueError):
    pass


class ErroSefaz(RuntimeError):
    pass


class NotaNaoReconhecida(ValueError):
    pass


def validar_link(url: str) -> str:
    """Aceita só https://nfeweb.sefaz.go.gov.br/... (sem usuário/senha nem porta estranha)."""
    url = (url or "").strip()
    if len(url) > 2048:
        raise LinkInvalido("Link longo demais.")
    try:
        partes = urlsplit(url)
        porta = partes.port
    except ValueError as e:
        raise LinkInvalido("Link malformado.") from e
    if partes.scheme != "https":
        raise LinkInvalido("O link da NFC-e precisa usar HTTPS.")
    if partes.username or partes.password or "@" in partes.netloc:
        raise LinkInvalido("Link com credenciais não é aceito.")
    if (partes.hostname or "").lower() != HOST_PERMITIDO:
        raise LinkInvalido(f"Só são aceitos links de {HOST_PERMITIDO} (NFC-e de Goiás).")
    if porta not in (None, 443):
        raise LinkInvalido("Porta não permitida.")
    return url


def chave_do_link(url: str) -> str | None:
    """No QR Code a chave vem no começo do parâmetro p (chave|versão|ambiente|...)."""
    try:
        q = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    for valor in q.get("p", []) + q.get("chNFe", []):
        m = re.match(r"\s*(\d{44})", valor)
        if m:
            return m.group(1)
    return None


def _get(client: httpx.Client, url: str) -> tuple[str, str]:
    """GET com limite de tamanho; cada redirecionamento passa de novo por validar_link."""
    for _ in range(MAX_REDIRECTS + 1):
        with client.stream("GET", url) as resp:
            if resp.is_redirect:
                url = validar_link(urljoin(url, resp.headers.get("location", "")))
                continue
            if resp.status_code != 200:
                raise ErroSefaz(f"A SEFAZ respondeu HTTP {resp.status_code}.")
            corpo = bytearray()
            for bloco in resp.iter_bytes():
                corpo.extend(bloco)
                if len(corpo) > MAX_BYTES:
                    raise ErroSefaz("Resposta da SEFAZ grande demais.")
            texto = bytes(corpo).decode(resp.charset_encoding or "utf-8", errors="replace")
            return texto, url
    raise ErroSefaz("Redirecionamentos demais.")


def html_do_render(xml: str) -> str:
    """O endpoint render/html devolve <Map><STATUS>…<DANFE_NFCE_HTML>(HTML escapado)</…></Map>."""
    m = BeautifulSoup(xml, "html.parser")
    status = m.find("status")
    if status and _texto(status).upper() != "SUCCESS":
        msg = _texto(m.find("message")) or _texto(status)
        raise ErroSefaz(f"A SEFAZ não retornou a nota ({msg}).")
    bloco = m.find("danfe_nfce_html")
    if not bloco:
        raise NotaNaoReconhecida("Resposta da SEFAZ sem o DANFE da NFC-e.")
    return bloco.get_text()


def _urls_conteudo(pagina: str, base: str) -> list[str]:
    """Na SEFAZ-GO a página do QR Code é só uma casca: os dados vêm de /nfce/render/… (mesma sessão)."""
    urls = re.findall(r"['\"](/nfeweb/sites/nfce/render/html/danfeNFCe\?chNFe=\d{44})['\"]", pagina)
    soup = BeautifulSoup(pagina, "html.parser")
    urls += [f["src"] for f in soup.select("iframe[src]") if "/render/" in f["src"]]
    chave = chave_do_link(base)
    if chave and not urls:
        urls.append(f"/nfeweb/sites/nfce/render/html/danfeNFCe?chNFe={chave}")
    return [urljoin(base, u) for u in urls]


def baixar_pagina(url: str, client: httpx.Client | None = None) -> str:
    """Devolve o HTML do DANFE NFC-e seguindo o fluxo da SEFAZ-GO, com SSL verificado.

    1. GET no link do QR Code (cria a sessão/cookie);
    2. GET em /nfeweb/sites/nfce/render/html/danfeNFCe?chNFe=… (XML com o HTML da nota)
       ou, se não houver, no iframe /render/danfeNFCe. Todo URL é validado (mesmo host, HTTPS).
    """
    url = validar_link(url)
    proprio = client is None
    client = client or httpx.Client(
        verify=True,
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=False,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml",
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
    )
    try:
        pagina, final = _get(client, url)
        if "tabResult" in pagina:
            return pagina
        for destino in _urls_conteudo(pagina, final):
            conteudo, _ = _get(client, validar_link(destino))
            if conteudo.lstrip().startswith("<Map") or "DANFE_NFCE_HTML" in conteudo:
                conteudo = html_do_render(conteudo)
            if "tabResult" in conteudo:
                return conteudo
        raise NotaNaoReconhecida("A SEFAZ não devolveu os itens da nota.")
    except httpx.HTTPError as e:
        raise ErroSefaz(f"Falha ao acessar a SEFAZ: {e.__class__.__name__}") from e
    finally:
        if proprio:
            client.close()


# ---------------------------------------------------------------- parser

def _texto(el: Tag | None) -> str:
    return limpar_espacos(el.get_text(" ")) if el else ""


def _valor_apos_rotulo(el: Tag | None) -> str:
    """'<strong>Qtde.:</strong>1,5' → '1,5' (texto do elemento sem o rótulo)."""
    if not el:
        return ""
    txt = _texto(el)
    rotulo = el.find(["strong", "label", "b"])
    if rotulo:
        txt = txt.replace(_texto(rotulo), "", 1)
    return txt.strip(" :")


def _limpar_nome(nome: str) -> str:
    nome = limpar_espacos(nome)
    nome = re.sub(r"\(\s*C[óo]digo\s*:.*?\)", "", nome, flags=re.I)
    return nome.strip(" -")


def _mercado(soup: BeautifulSoup) -> str:
    for sel in ("#u20", ".txtTopo", "#emitente", ".NFCCabecalho_SubTitulo"):
        el = soup.select_one(sel)
        if el and _texto(el):
            return _texto(el)
    return ""


def _cnpj(soup: BeautifulSoup, texto: str) -> str:
    m = re.search(r"CNPJ\s*:?\s*(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})", texto, re.I)
    return so_digitos(m.group(1)) if m else ""


def _data(texto: str) -> datetime | None:
    m = re.search(
        r"Emiss[ãa]o\s*:?\s*(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?", texto, re.I
    ) or re.search(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?", texto)
    if not m:
        return None
    d, mo, a, h, mi, s = m.groups()
    try:
        return datetime(int(a), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), tzinfo=TZ)
    except ValueError:
        return None


def _chave(soup: BeautifulSoup, texto: str, url: str | None) -> str:
    el = soup.select_one(".chave")
    candidatos = []
    if el:
        candidatos.append(so_digitos(_texto(el)))
    candidatos += [so_digitos(m) for m in re.findall(r"(?:\d{4}\s?){10}\d{4}", texto)]
    if url:
        candidatos.append(chave_do_link(url) or "")
    for c in candidatos:
        if chave_valida(c):
            return c
    return next((c for c in candidatos if len(c) == 44), "")


def _linha_total(soup: BeautifulSoup, texto: str, rotulo: str) -> float | None:
    """Valor de uma linha do bloco de totais ('Valor a pagar R$:', 'Descontos R$:')."""
    for linha in soup.select("#totalNota #linhaTotal, #totalNota div, #linhaTotal"):
        if re.search(rotulo, _texto(linha), re.I):
            valor = linha.select_one(".totalNumb, .txtMax") or linha
            v = parse_numero_br(_texto(valor).split("$")[-1])
            if v is not None:
                return v
    m = re.search(rotulo + r"\s*R?\$?\s*:?\s*([\d.]+,\d{2})", texto, re.I)
    if m:
        return parse_numero_br(m.group(1))
    return None


def _itens(soup: BeautifulSoup) -> list[dict]:
    linhas = soup.select("#tabResult tr") or soup.select("tr[id^=Item]")
    itens = []
    for tr in linhas:
        nome_el = tr.select_one(".txtTit, .txtTit2")
        if not nome_el:
            continue
        nome = _limpar_nome(_texto(nome_el))
        if not nome:
            continue
        codigo = so_digitos(_texto(tr.select_one(".RCod"))) or None
        qtd = parse_numero_br(_valor_apos_rotulo(tr.select_one(".Rqtd"))) or 1.0
        unidade = _valor_apos_rotulo(tr.select_one(".RUN")).upper() or "UN"
        unit = parse_numero_br(_valor_apos_rotulo(tr.select_one(".RvlUnit")))
        total = parse_numero_br(_texto(tr.select_one(".valor")))
        if total is None and unit is not None:
            total = round(unit * qtd, 2)
        if unit is None and total is not None:
            unit = round(total / qtd, 4) if qtd else total
        if total is None:
            continue
        itens.append({
            "nome": nome,
            "codigo": codigo,
            "ean": codigo if gtin_valido(codigo) else None,
            "qtd": qtd,
            "unidade": unidade[:10],
            "preco_unit": unit,
            "preco_total": total,
            "categoria": categorizar(nome),
        })
    return itens


def parse_nfce_html(html: str, url: str | None = None) -> dict:
    """Extrai os dados da página de consulta da NFC-e. Lança NotaNaoReconhecida se não houver itens."""
    soup = BeautifulSoup(html, "html.parser")
    for lixo in soup(["script", "style", "noscript"]):
        lixo.decompose()
    texto = _texto(soup)

    itens = _itens(soup)
    if not itens:
        raise NotaNaoReconhecida("Não encontrei itens na página da NFC-e.")

    chave = _chave(soup, texto, url)
    cnpj = _cnpj(soup, texto) or (cnpj_da_chave(chave) if chave_valida(chave) else "")
    data = _data(texto)
    soma = round(sum(i["preco_total"] for i in itens), 2)
    total = _linha_total(soup, texto, r"valor\s+a\s+pagar")
    descontos = _linha_total(soup, texto, r"descontos?") or 0.0
    return {
        "mercado": _mercado(soup),
        "cnpj": cnpj,
        "data": data.isoformat() if data else None,
        "chave": chave,
        "total": total if total is not None else round(soma - descontos, 2),
        "descontos": descontos,
        "itens": itens,
    }

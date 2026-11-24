"""nfce_scanner.py - leitor de NFC-e de Goiás em um único arquivo (extraído do SmartMercado).

O que faz
  * ler_link(url)        URL do QR Code (https://nfeweb.sefaz.go.gov.br/...) -> nota estruturada
  * ler_foto_bytes(img)  foto do cupom -> nota estruturada, usando o Gemini (JSON + response_schema)
  * categorizar(nome)    categoria local por palavras inteiras (sem IA)
  * criar_router()       APIRouter do FastAPI pronto (POST /nfce/link, POST /nfce/foto, GET /nfce/categorias)

Formato da nota devolvida (dict JSON-serializável)
  {"mercado", "cnpj" (14 dígitos), "data" (ISO, -03:00, ou None), "chave" (44 dígitos ou ""),
   "total" (valor a pagar, já com descontos), "descontos", "origem" ("qrcode"|"foto"),
   "soma_itens", "diferenca", "soma_confere",
   "itens": [{"nome", "codigo", "ean", "qtd", "unidade", "preco_unit", "preco_total", "categoria"}]}

Dependências:  pip install httpx beautifulsoup4 google-genai      (+ fastapi python-multipart para o router)
Variáveis:     GEMINI_API_KEY (obrigatória para foto), GEMINI_MODEL (opcional, padrão gemini-3.8-flash)
Teste rápido:  python nfce_scanner.py "https://nfeweb.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe?p=..."

Ele NÃO grava em banco, NÃO tem login e NÃO tem tela: isso fica com o seu aplicativo.
Só aceita links https://nfeweb.sefaz.go.gov.br, com verificação SSL ligada.
"""

import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag


# ============================================================================
# Utilitários (números pt-BR, texto, GTIN, chave de acesso)
# ============================================================================

TZ = ZoneInfo("America/Sao_Paulo")


def parse_numero_br(texto: str | None) -> float | None:
    """Converte '1.234,56' / '27,99' / '0,345' em float. Retorna None se não houver número."""
    if texto is None:
        return None
    m = re.search(r"-?\d[\d.]*(?:,\d+)?|-?\d+(?:\.\d+)?", texto.replace("\xa0", " "))
    if not m:
        return None
    s = m.group(0)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def remover_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def normalizar_nome(texto: str) -> str:
    """Maiúsculas, sem acentos, só letras/números separados por um espaço."""
    t = remover_acentos(texto).upper()
    t = re.sub(r"[^A-Z0-9]+", " ", t)
    return t.strip()


def limpar_espacos(texto: str) -> str:
    return re.sub(r"\s+", " ", texto.replace("\xa0", " ")).strip()


def so_digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


def gtin_valido(codigo: str | None) -> bool:
    """Valida EAN-8/UPC-12/EAN-13/GTIN-14 pelo dígito verificador."""
    if not codigo or not codigo.isdigit() or len(codigo) not in (8, 12, 13, 14):
        return False
    if set(codigo) == {"0"}:
        return False
    corpo, dv = codigo[:-1], int(codigo[-1])
    soma = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(corpo)))
    return (10 - soma % 10) % 10 == dv


def dv_chave(chave43: str) -> int:
    """Dígito verificador (módulo 11) da chave de acesso NF-e/NFC-e."""
    pesos = [2, 3, 4, 5, 6, 7, 8, 9]
    soma = sum(int(d) * pesos[i % 8] for i, d in enumerate(reversed(chave43)))
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def chave_valida(chave: str | None) -> bool:
    return bool(chave) and len(chave) == 44 and chave.isdigit() and set(chave) != {"0"} and dv_chave(chave[:43]) == int(chave[43])


def chave_sintetica(texto: str) -> str:
    """ID de 44 dígitos para cupom sem chave fiscal. Começa com 00 (chave real começa com o código da UF)."""
    return "00" + str(int(hashlib.sha256(texto.encode()).hexdigest(), 16))[:42].zfill(42)


def chave_aceita(chave: str) -> bool:
    return chave_valida(chave) or (len(chave) == 44 and chave.isdigit() and chave.startswith("00") and set(chave) != {"0"})


def cnpj_da_chave(chave: str) -> str:
    return chave[6:20]


def formatar_cnpj(cnpj: str) -> str:
    d = so_digitos(cnpj)
    if len(d) != 14:
        return cnpj
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def para_sp(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


# ============================================================================
# Categorização local por palavras inteiras
# ============================================================================

OUTROS = "Outros"

PALAVRAS: dict[str, list[str]] = {
    "Hortifruti": [
        "BANANA", "MACA", "LARANJA", "LIMAO", "TOMATE", "BATATA", "BATATA DOCE", "CEBOLA", "ALHO",
        "CENOURA", "ALFACE", "MAMAO", "MELANCIA", "MELAO", "UVA", "MORANGO", "ABACAXI", "MANGA",
        "PEPINO", "PIMENTAO", "ABOBRINHA", "ABOBORA", "CHUCHU", "REPOLHO", "BROCOLIS", "COUVE",
        "MANDIOCA", "INHAME", "BETERRABA", "QUIABO", "JILO", "VAGEM", "COENTRO", "CHEIRO VERDE",
        "CEBOLINHA", "SALSA", "ABACATE", "GOIABA", "MARACUJA", "KIWI", "PERA", "TANGERINA",
        "PONKAN", "BATATINHA", "HORTIFRUTI", "FLV", "OVO", "OVOS", "OVOS BRANCOS", "OVOS VERMELHOS",
    ],
    "Carnes": [
        "CARNE", "CARNE MOIDA", "BOVINA", "BOV", "FRANGO", "FGO", "PEITO FGO", "PEITO FRANGO",
        "COXA", "SOBRECOXA", "ASA", "COXINHA ASA", "LINGUICA", "LING", "SALSICHA", "BACON",
        "PATINHO", "ACEM", "ALCATRA", "PICANHA", "COSTELA", "SUINO", "SUINA", "PERNIL", "LOMBO",
        "TILAPIA", "PEIXE", "FILE", "CONTRA FILE", "MAMINHA", "CUPIM", "FRALDINHA", "MUSCULO",
        "COXAO", "COXAO MOLE", "COXAO DURO", "HAMBURGUER", "ALMONDEGA", "CALABRESA", "TOSCANA",
        "MIUDOS", "FIGADO", "SARDINHA FRESCA",
    ],
    "Frios e Laticínios": [
        "LEITE", "LTE", "LT INTEGRAL", "LT DESN", "LT UHT", "LEITE UHT", "QUEIJO", "QJO",
        "MUSSARELA", "MUCARELA", "MUSS", "PRATO", "QJO PRATO", "PRESUNTO", "APRESUNTADO", "APRES",
        "MORTADELA", "MORT", "SALAME", "PEITO PERU", "PEITO DE PERU", "IOGURTE", "IOG", "REQUEIJAO",
        "REQ", "MANTEIGA", "MANT", "MARGARINA", "MARG", "NATA", "COALHADA", "BEBIDA LACTEA",
        "LEITE FERM", "LEITE FERMENTADO", "BEB LACTEA", "BEB LACT", "PETIT SUISSE", "CREAM CHEESE", "RICOTA", "PARMESAO",
    ],
    "Padaria": [
        "PAO", "PAES", "PAO FORMA", "PAO FRANCES", "PAO DE QUEIJO", "PAO QUEIJO", "BISNAGUINHA",
        "ROSCA", "BOLO", "TORRADA", "SONHO", "BROA", "CROISSANT", "PANETONE",
    ],
    "Mercearia": [
        "ARROZ", "FEIJAO", "FEIJ", "MACARRAO", "MAC INSTANT", "ESPAGUETE", "PENNE", "PARAFUSO",
        "ACUCAR", "ACUC", "ADOCANTE", "OLEO", "OLEO SOJA", "AZEITE", "SAL", "SAL REFINADO", "CAFE",
        "FARINHA", "FAR TRIGO", "FAR MANDIOCA", "FUBA", "AVEIA", "BISC", "BISCOITO", "BOLACHA",
        "WAFER", "MOLHO", "MOLHO TOMATE", "EXTRATO", "EXTRATO TOMATE", "MAIONESE", "KETCHUP",
        "CATCHUP", "MOSTARDA", "TEMPERO", "VINAGRE", "MILHO", "MILHO VERDE", "ERVILHA", "SARDINHA",
        "ATUM", "ACHOC", "ACHOCOLATADO", "LEITE CONDENSADO", "LEITE COND", "LT COND", "CREME DE LEITE",
        "CR LEITE", "CREME LEITE", "LEITE DE COCO", "LEITE COCO", "LEITE EM PO", "LEITE PO", "LT PO",
        "GELATINA", "CHOCOLATE", "CHOC", "BOMBOM", "MIOJO", "LAMEN", "MASSA", "GRANOLA", "CEREAL",
        "PIPOCA", "AMENDOIM", "BATATA PALHA", "BATATA CHIPS", "SALGADINHO", "DOCE", "GELEIA", "MEL",
        "COLORAU", "CALDO", "SOPA", "AZEITONA", "PALMITO", "CANJICA", "TAPIOCA", "GOMA", "POLVILHO",
        "FERMENTO", "CHA", "CHA MATE", "BALA", "CHICLETE", "PAPINHA", "MISTURA BOLO",
        "MIST BOLO", "NUTELLA", "CREME DE AVELA", "MISTURA P BOLO", "MISTURA PARA BOLO", "SHOYU", "AJINOSHOYU",
    ],
    "Bebidas": [
        "REFRIG", "REFRIGERANTE", "REFRI", "BEB", "BEBIDA", "COCA", "COCA COLA", "PEPSI", "GUARANA", "FANTA", "SPRITE",
        "SUCO", "NECTAR", "AGUA", "AGUA MIN", "AGUA MINERAL", "AGUA DE COCO", "AGUA COCO", "ENERG",
        "ENERGETICO", "ISOTONICO", "TONICA", "H2OH", "CHA GELADO", "CHA PRONTO",
    ],
    "Bebidas Alcoólicas": [
        "CERV", "CERVEJA", "VINHO", "VODKA", "WHISKY", "CACHACA", "GIN", "RUM", "LICOR", "ESPUMANTE",
        "SKOL", "BRAHMA", "HEINEKEN", "AMSTEL", "BUDWEISER", "SPATEN", "STELLA", "ITAIPAVA",
        "ANTARCTICA", "CHOPP", "CHOPE", "SIDRA", "BEATS",
    ],
    "Higiene": [
        "SABONETE", "SABONET", "SAB LIQ", "SAB LIQUIDO", "SHAMPOO", "SHAMP", "CONDICIONADOR",
        "CONDIC", "CREME DENTAL", "CR DENT", "CR DENTAL", "ESC DENT", "ESC DENTAL", "ESCOVA DENTAL",
        "ESCOVA DENTE", "FIO DENTAL", "FIO DENT", "DESOD", "DESODORANTE", "ANTITRANSP",
        "PAPEL HIG", "PAP HIG", "PAPEL HIGIENICO", "ABSORVENTE", "ABSORV", "FRALDA", "COTONETE",
        "HASTES FLEX", "APARELHO BARBEAR", "APAR BARBEAR", "HIDRATANTE", "PROT SOLAR",
        "PROTETOR SOLAR", "ALGODAO", "ENXAGUANTE BUCAL", "ENXAG", "LENCO UMED", "LENCO UMEDECIDO",
        "CREME CABELO", "CR PENTEAR", "GEL CABELO",
    ],
    "Limpeza": [
        "DETERGENTE", "DET", "LAVA LOUCAS", "LAVA LOUCA", "SABAO", "SAB PO", "SAB EM PO", "SABAO PO",
        "SABAO EM PO", "SAB BARRA", "LAVA ROUPAS", "LAVA ROUPA", "AMACIANTE", "AMAC", "DESINFETANTE",
        "DESINF", "AGUA SANITARIA", "AG SANIT", "AG SANITARIA", "ALVEJANTE", "ALVEJ", "CLORO",
        "MULTIUSO", "MULTI USO", "LIMPA VIDRO", "LUSTRA MOVEIS", "ESPONJA", "PALHA ACO", "PALHA DE ACO",
        "BOMBRIL", "SACO LIXO", "SACO P LIXO", "SACO DE LIXO", "SC LIXO", "VASSOURA", "RODO",
        "PANO", "PANO CHAO", "FLANELA", "INSETICIDA", "LIMPADOR", "SAPONACEO", "TIRA MANCHAS",
        "PAPEL TOALHA", "PAP TOALHA", "LUVA",
    ],
    "Utilidades": [
        "PAPEL ALUMINIO", "PAPEL ALUM", "FILME PVC", "COPO DESC", "COPO DESCARTAVEL", "PRATO DESC",
        "PILHA", "VELA", "FOSFORO", "CARVAO", "GUARDANAPO", "LAMPADA", "ISQUEIRO", "PALITO",
        "SACO FREEZER", "SACO PAO", "FITA", "GAS",
    ],
    "Pet": [
        "RACAO", "AREIA GATO", "AREIA SANIT", "AREIA HIGIENICA", "PETISCO", "SACHE", "PEDIGREE",
        "WHISKAS", "GOLDEN", "PREMIER PET",
    ],
}

CATEGORIAS: list[str] = list(PALAVRAS.keys()) + [OUTROS]


def _montar():
    frases: list[tuple[re.Pattern, str]] = []
    palavras: dict[str, str] = {}
    for cat, termos in PALAVRAS.items():
        for termo in termos:
            t = normalizar_nome(termo)
            if " " in t:
                frases.append((t, cat))
            else:
                palavras.setdefault(t, cat)
    frases.sort(key=lambda fc: len(fc[0]), reverse=True)
    frases_re = [(re.compile(r"\b" + re.escape(f) + r"\b"), c) for f, c in frases]
    # Alternativas mais longas primeiro: na mesma posição, a regex fica com a maior.
    alternativas = sorted(palavras, key=len, reverse=True)
    palavras_re = re.compile(r"\b(" + "|".join(re.escape(p) for p in alternativas) + r")\b")
    return frases_re, palavras_re, palavras


_FRASES, _PALAVRAS_RE, _PALAVRA_CAT = _montar()


def categorizar(nome: str) -> str:
    texto = normalizar_nome(nome or "")
    if not texto:
        return OUTROS
    for padrao, cat in _FRASES:
        if padrao.search(texto):
            return cat
    m = _PALAVRAS_RE.search(texto)
    if m:
        return _PALAVRA_CAT[m.group(1)]
    return OUTROS


# ============================================================================
# NFC-e de Goiás: validação do link, download e parser
# ============================================================================

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


# ============================================================================
# Leitura por foto com o Gemini
# ============================================================================

MODELO_PADRAO = "gemini-3.8-flash"
MODELOS_RESERVA = ("gemini-3.7-flash", "gemini-3.6-flash")
PRAZO_SEG = 45
MAX_IMAGEM = 4 * 1024 * 1024
TIPOS_IMAGEM = {"image/jpeg", "image/png", "image/webp"}


class ErroGemini(RuntimeError):
    pass


SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "mercado": {"type": "STRING", "description": "Nome/razão social do emitente"},
        "cnpj": {"type": "STRING", "nullable": True, "description": "CNPJ do emitente, só dígitos"},
        "data_emissao": {
            "type": "STRING",
            "nullable": True,
            "description": "Data e hora de emissão no formato AAAA-MM-DDTHH:MM:SS",
        },
        "chave_acesso": {
            "type": "STRING",
            "nullable": True,
            "description": "Chave de acesso com 44 dígitos, sem espaços",
        },
        "valor_a_pagar": {"type": "NUMBER", "description": "Valor a pagar (já com descontos)"},
        "descontos": {"type": "NUMBER", "nullable": True},
        "itens": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "nome": {"type": "STRING"},
                    "codigo": {"type": "STRING", "nullable": True},
                    "quantidade": {"type": "NUMBER"},
                    "unidade": {"type": "STRING", "nullable": True},
                    "preco_unitario": {"type": "NUMBER"},
                    "preco_total": {"type": "NUMBER"},
                },
                "required": ["nome", "quantidade", "preco_unitario", "preco_total"],
            },
        },
    },
    "required": ["mercado", "valor_a_pagar", "itens"],
}

PROMPT = """Você recebe a foto de um cupom NFC-e (DANFE NFC-e) de um supermercado brasileiro.
Extraia exatamente o que está impresso, sem inventar:
- mercado: nome do emitente; cnpj: só dígitos.
- data_emissao: data/hora de emissão (AAAA-MM-DDTHH:MM:SS); null se o cupom não tiver data.
- chave_acesso: os 44 dígitos da chave de acesso, sem espaços. Cupons "NÃO É DOCUMENTO FISCAL" não têm chave: use null, nunca invente dígitos.
- valor_a_pagar: o "Valor a pagar" ou "TOTAL" (já com descontos); descontos: total de descontos, se houver.
- itens: um por linha do cupom, na ordem. Números com ponto decimal (27.99, não 27,99).
  preco_total é o valor total da linha. Mantenha o nome como impresso.
Se algum campo estiver ilegível, use null (nos opcionais)."""


def _num(valor) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def normalizar_resposta(bruto: dict) -> dict:
    """Converte o JSON do Gemini no mesmo formato do parser de NFC-e."""
    itens = []
    for i in bruto.get("itens") or []:
        nome = limpar_espacos(str(i.get("nome") or ""))
        qtd = _num(i.get("quantidade")) or 1.0
        total = _num(i.get("preco_total"))
        unit = _num(i.get("preco_unitario"))
        if not nome or total is None:
            continue
        if unit is None:
            unit = round(total / qtd, 4)
        codigo = so_digitos(str(i.get("codigo") or "")) or None
        itens.append({
            "nome": nome[:200],
            "codigo": codigo,
            "ean": codigo if gtin_valido(codigo) else None,
            "qtd": qtd,
            "unidade": (limpar_espacos(str(i.get("unidade") or "UN")).upper() or "UN")[:10],
            "preco_unit": unit,
            "preco_total": round(total, 2),
            "categoria": categorizar(nome),
        })

    chave = so_digitos(str(bruto.get("chave_acesso") or ""))
    if not chave_valida(chave):
        chave = ""  # cupom não fiscal ou leitura errada: o servidor gera um ID
    cnpj = so_digitos(str(bruto.get("cnpj") or ""))
    if len(cnpj) != 14 and chave_valida(chave):
        cnpj = cnpj_da_chave(chave)

    data = None
    if bruto.get("data_emissao"):
        try:
            dt = datetime.fromisoformat(str(bruto["data_emissao"]))
            data = (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).isoformat()
        except ValueError:
            data = None

    soma = round(sum(i["preco_total"] for i in itens), 2)
    descontos = _num(bruto.get("descontos")) or 0.0
    total = _num(bruto.get("valor_a_pagar"))
    return {
        "mercado": limpar_espacos(str(bruto.get("mercado") or "")),
        "cnpj": cnpj if len(cnpj) == 14 else "",
        "data": data,
        "chave": chave,
        "total": round(total, 2) if total is not None else round(soma - descontos, 2),
        "descontos": round(descontos, 2),
        "itens": itens,
    }


def ler_foto(imagem: bytes, mime: str) -> dict:
    """Envia a imagem ao Gemini e devolve a nota normalizada. Chamada bloqueante."""
    from google import genai
    from google.genai import types

    chave_api = os.environ.get("GEMINI_API_KEY")
    if not chave_api:
        raise ErroGemini("GEMINI_API_KEY não configurada.")
    client = genai.Client(api_key=chave_api)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=SCHEMA,
        temperature=0,
    )
    # Principal primeiro; se estiver sobrecarregado (503) ou sem cota (429), tenta os reservas.
    modelos = list(dict.fromkeys([os.environ.get("GEMINI_MODEL", MODELO_PADRAO), *MODELOS_RESERVA]))
    limite = time.monotonic() + PRAZO_SEG  # a função do Vercel tem 60 s
    ultimo_erro = ""
    for modelo in modelos:
        for tentativa in range(2):
            if time.monotonic() > limite:
                break
            try:
                resp = client.models.generate_content(
                    model=modelo,
                    contents=[types.Part.from_bytes(data=imagem, mime_type=mime), PROMPT],
                    config=config,
                )
                return normalizar_resposta(json.loads(resp.text))
            except json.JSONDecodeError as e:
                raise ErroGemini("O Gemini não devolveu um JSON válido. Tente outra foto.") from e
            except Exception as e:  # erros de rede/cota do SDK
                ultimo_erro = str(e)
                if not any(c in ultimo_erro for c in ("503", "429", "404", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "NOT_FOUND")):
                    raise ErroGemini(f"Falha ao ler a foto com o Gemini ({e.__class__.__name__}).") from e
                if tentativa == 0:
                    time.sleep(2)
    if "429" in ultimo_erro or "RESOURCE_EXHAUSTED" in ultimo_erro:
        raise ErroGemini("A cota gratuita do Gemini acabou (limite por minuto ou por dia). Espere um pouco ou use o QR Code.")
    raise ErroGemini("O Gemini está sobrecarregado agora. Tente de novo em instantes ou use o QR Code.")


def tipo_imagem(dados: bytes) -> str | None:
    """Detecta o tipo pela assinatura do arquivo (não confia no content-type do cliente)."""
    if dados.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if dados.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if dados[:4] == b"RIFF" and dados[8:12] == b"WEBP":
        return "image/webp"
    return None


# ============================================================================
# API pública do módulo
# ============================================================================


TOLERANCIA = 0.05  # R$ de diferença aceita entre soma dos itens (- descontos) e o total


def conferir_soma(nota: dict) -> dict:
    """Acrescenta soma_itens, diferenca e soma_confere (soma - descontos ~ total)."""
    soma = round(sum(float(i.get("preco_total") or 0) for i in nota.get("itens", [])), 2)
    descontos = float(nota.get("descontos") or 0)
    total = float(nota.get("total") or 0)
    dif = round(soma - descontos - total, 2)
    nota["soma_itens"] = soma
    nota["diferenca"] = dif
    nota["soma_confere"] = abs(dif) <= TOLERANCIA
    return nota


class ImagemInvalida(ValueError):
    pass


def ler_link(url: str) -> dict:
    """URL do QR Code -> nota. Lança LinkInvalido, ErroSefaz ou NotaNaoReconhecida."""
    url = validar_link(url)
    nota = parse_nfce_html(baixar_pagina(url), url)
    nota["origem"] = "qrcode"
    return conferir_soma(nota)


def ler_foto_bytes(imagem: bytes) -> dict:
    """Foto (JPEG/PNG/WEBP até 4 MB) -> nota. Lança ImagemInvalida, ErroGemini ou NotaNaoReconhecida."""
    if len(imagem) > MAX_IMAGEM:
        raise ImagemInvalida("Imagem grande demais (máx. 4 MB).")
    mime = tipo_imagem(imagem)
    if mime not in TIPOS_IMAGEM:
        raise ImagemInvalida("Envie uma imagem JPEG, PNG ou WEBP.")
    nota = ler_foto(imagem, mime)
    if not nota["itens"]:
        raise NotaNaoReconhecida("Não consegui ler itens na foto. Tente outra mais nítida.")
    nota["origem"] = "foto"
    return conferir_soma(nota)


def criar_router(dependencies=None):
    """APIRouter do FastAPI. Passe `dependencies=[Depends(sua_autenticacao)]`: sem isso a rota fica aberta
    e qualquer um poderia gastar sua cota do Gemini."""
    from fastapi import APIRouter, File, HTTPException, UploadFile
    from pydantic import BaseModel

    class LinkIn(BaseModel):
        url: str

    router = APIRouter(prefix="/nfce", dependencies=dependencies or [])

    @router.get("/categorias")
    def _categorias():
        return CATEGORIAS

    @router.post("/link")
    def _link(body: LinkIn):  # def síncrona: roda no threadpool, não trava o event loop
        try:
            return ler_link(body.url)
        except LinkInvalido as e:
            raise HTTPException(400, str(e))
        except ErroSefaz as e:
            raise HTTPException(502, f"{e} Tente pela foto da nota.")
        except NotaNaoReconhecida as e:
            raise HTTPException(422, f"{e} Tente pela foto da nota.")

    @router.post("/foto")
    def _foto(foto: UploadFile = File(...)):
        try:
            return ler_foto_bytes(foto.file.read(MAX_IMAGEM + 1))
        except ImagemInvalida as e:
            raise HTTPException(413 if "grande" in str(e) else 415, str(e))
        except ErroGemini as e:
            raise HTTPException(502, str(e))
        except NotaNaoReconhecida as e:
            raise HTTPException(422, str(e))

    return router


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("uso: python nfce_scanner.py <url-do-qrcode>")
    print(json.dumps(ler_link(sys.argv[1]), ensure_ascii=False, indent=2))

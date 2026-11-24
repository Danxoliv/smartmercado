"""Funções utilitárias compartilhadas (números pt-BR, normalização de texto, GTIN, chave)."""

import hashlib
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

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


def cnpj_valido(cnpj: str | None) -> bool:
    """14 dígitos com os dois dígitos verificadores corretos (pega CNPJ mal lido de foto)."""
    if not cnpj or len(cnpj) != 14 or not cnpj.isdigit() or len(set(cnpj)) == 1:
        return False
    d = [int(x) for x in cnpj]
    for n, pesos in ((12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]), (13, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])):
        resto = sum(x * p for x, p in zip(d[:n], pesos)) % 11
        if d[n] != (0 if resto < 2 else 11 - resto):
            return False
    return True


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

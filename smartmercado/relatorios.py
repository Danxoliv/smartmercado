"""Painel mensal, comparador de preços e exportação CSV (funções puras sobre a lista de notas)."""

import csv
import io
from collections import defaultdict
from datetime import datetime

from .util import formatar_cnpj, normalizar_nome, para_sp, so_digitos


def _data(nota: dict) -> datetime:
    return para_sp(nota["data"])


def _fator_desconto(nota: dict) -> float:
    """Distribui o desconto da nota proporcionalmente entre os itens (soma dos itens → total pago)."""
    soma = sum(float(i.get("preco_total") or 0) for i in nota.get("itens", []))
    total = float(nota.get("total") or 0)
    return total / soma if soma > 0 else 1.0


def nota_publica(n: dict) -> dict:
    return {**{k: v for k, v in n.items() if k != "criado_em"}, "data": _data(n).isoformat()}


def painel(notas: list[dict]) -> dict:
    por_cat: dict[str, float] = defaultdict(float)
    por_mercado: dict[str, dict] = {}
    compras = []
    total = 0.0
    for n in sorted(notas, key=_data, reverse=True):
        valor = float(n.get("total") or 0)
        total += valor
        fator = _fator_desconto(n)
        for i in n.get("itens", []):
            por_cat[i.get("categoria") or "Outros"] += float(i.get("preco_total") or 0) * fator
        chave_m = n.get("cnpj") or normalizar_nome(n.get("mercado", ""))
        m = por_mercado.setdefault(chave_m, {"mercado": n.get("mercado", ""), "total": 0.0, "compras": 0})
        m["total"] += valor
        m["compras"] += 1
        compras.append({
            "chave": n["chave"],
            "mercado": n.get("mercado", ""),
            "data": _data(n).isoformat(),
            "total": round(valor, 2),
            "qtd_itens": len(n.get("itens", [])),
            "origem": n.get("origem"),
        })
    return {
        "total": round(total, 2),
        "qtd_compras": len(notas),
        "por_categoria": sorted(
            ({"categoria": c, "total": round(v, 2)} for c, v in por_cat.items()),
            key=lambda x: x["total"], reverse=True,
        ),
        "por_mercado": sorted(
            ({**m, "total": round(m["total"], 2)} for m in por_mercado.values()),
            key=lambda x: x["total"], reverse=True,
        ),
        "compras": compras,
    }


def meses_ate(mes: str, n: int = 6) -> list[str]:
    """Os n meses (AAAA-MM) terminando em `mes`, do mais antigo ao mais novo."""
    ano, m = int(mes[:4]), int(mes[5:])
    meses = []
    for k in range(n - 1, -1, -1):
        total = ano * 12 + (m - 1) - k
        meses.append(f"{total // 12:04d}-{total % 12 + 1:02d}")
    return meses


def comparativo(notas: list[dict], mes: str, n: int = 6) -> dict:
    """Evolução mensal, variação contra o mês anterior e categorias do mês vs. anterior."""
    meses = meses_ate(mes, n)
    por_mes = {c: {"mes": c, "total": 0.0, "compras": 0} for c in meses}
    cats: dict[str, dict[str, float]] = {meses[-1]: defaultdict(float), meses[-2]: defaultdict(float)}
    for nota in notas:
        c = _data(nota).strftime("%Y-%m")
        if c not in por_mes:
            continue
        por_mes[c]["total"] += float(nota.get("total") or 0)
        por_mes[c]["compras"] += 1
        if c in cats:
            fator = _fator_desconto(nota)
            for i in nota.get("itens", []):
                cats[c][i.get("categoria") or "Outros"] += float(i.get("preco_total") or 0) * fator
    atual, anterior = por_mes[meses[-1]]["total"], por_mes[meses[-2]]["total"]
    nomes = set(cats[meses[-1]]) | set(cats[meses[-2]])
    return {
        "meses": [{**m, "total": round(m["total"], 2)} for m in por_mes.values()],
        "atual": round(atual, 2),
        "anterior": round(anterior, 2),
        "variacao_pct": round((atual - anterior) / anterior * 100, 1) if anterior else None,
        "ticket_medio": round(atual / por_mes[meses[-1]]["compras"], 2) if por_mes[meses[-1]]["compras"] else 0,
        "categorias": sorted(
            ({"categoria": c, "atual": round(cats[meses[-1]][c], 2), "anterior": round(cats[meses[-2]][c], 2)} for c in nomes),
            key=lambda x: max(x["atual"], x["anterior"]), reverse=True,
        ),
    }


def chave_produto(item: dict) -> str:
    """Agrupa pelo EAN quando existe; senão pelo nome normalizado."""
    return f"ean:{item['ean']}" if item.get("ean") else f"nome:{normalizar_nome(item.get('nome', ''))}"


def comparar(notas: list[dict], busca: str, limite: int = 30) -> list[dict]:
    termos = normalizar_nome(busca).split()
    digitos = so_digitos(busca)
    if not termos:
        return []
    grupos: dict[str, dict] = {}
    for n in notas:
        data = _data(n)
        mercado = n.get("mercado", "")
        for i in n.get("itens", []):
            nome_n = normalizar_nome(i.get("nome", ""))
            palavras = set(nome_n.split())
            casa = all(any(p.startswith(t) for p in palavras) for t in termos)
            if not casa and not (len(digitos) >= 8 and digitos in (i.get("ean"), i.get("codigo"))):
                continue
            g = grupos.setdefault(chave_produto(i), {
                "grupo": chave_produto(i), "nome": i["nome"], "ean": i.get("ean"),
                "_ultima": data, "historico": [], "categoria": i.get("categoria"),
            })
            if data >= g["_ultima"]:
                g["_ultima"], g["nome"], g["categoria"] = data, i["nome"], i.get("categoria")
            g["historico"].append({
                "data": data.isoformat(),
                "mercado": mercado,
                "preco_unit": round(float(i.get("preco_unit") or 0), 4),
                "unidade": i.get("unidade") or "UN",
                "qtd": i.get("qtd"),
                "_dt": data,
            })

    resultado = []
    for g in grupos.values():
        hist = sorted(g["historico"], key=lambda h: h["_dt"], reverse=True)
        por_mercado: dict[str, dict] = {}
        for h in hist:  # mais recente primeiro
            m = por_mercado.get(h["mercado"])
            if m is None:
                por_mercado[h["mercado"]] = {
                    "mercado": h["mercado"], "ultimo_preco": h["preco_unit"], "data": h["data"],
                    "menor_preco": h["preco_unit"], "maior_preco": h["preco_unit"], "vezes": 1,
                }
            else:
                m["menor_preco"] = min(m["menor_preco"], h["preco_unit"])
                m["maior_preco"] = max(m["maior_preco"], h["preco_unit"])
                m["vezes"] += 1
        for h in hist:
            del h["_dt"]
        resultado.append({
            "grupo": g["grupo"],
            "nome": g["nome"],
            "ean": g["ean"],
            "categoria": g["categoria"],
            "por_mercado": sorted(por_mercado.values(), key=lambda m: m["ultimo_preco"]),
            "historico": hist,
        })
    resultado.sort(key=lambda r: len(r["historico"]), reverse=True)
    return resultado[:limite]


def _br(v: float | None, casas: int = 2) -> str:
    if v is None:
        return ""
    return f"{v:.{casas}f}".replace(".", ",")


def _celula(v: str) -> str:
    """Evita injeção de fórmula ao abrir no Excel/Sheets."""
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


COLUNAS = [
    "data", "hora", "estabelecimento", "tipo_estabelecimento", "cnpj", "chave_acesso", "produto", "ean", "codigo", "categoria",
    "quantidade", "unidade", "preco_unitario", "preco_total", "total_nota", "origem",
]


def gerar_csv(notas: list[dict]) -> str:
    """CSV para Excel pt-BR: separador ';', decimal ',', UTF-8 com BOM, uma linha por item."""
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(COLUNAS)
    for n in sorted(notas, key=_data):
        d = _data(n)
        for i in n.get("itens", []):
            w.writerow([_celula(str(x)) for x in (
                d.strftime("%d/%m/%Y"), d.strftime("%H:%M"), n.get("estabelecimento") or n.get("mercado", ""),
                n.get("tipo_estabelecimento") or "", formatar_cnpj(n.get("cnpj") or ""), n["chave"], i.get("nome", ""),
                i.get("ean") or "", i.get("codigo") or "", i.get("categoria", ""),
                _br(i.get("qtd"), 3), i.get("unidade", ""), _br(i.get("preco_unit"), 2),
                _br(i.get("preco_total"), 2), _br(n.get("total"), 2), n.get("origem", ""),
            )])
    return buf.getvalue()

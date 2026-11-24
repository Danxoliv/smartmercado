"""Fase 5: checagens estáticas do frontend (CSP, sem innerHTML, ids ligados, service worker) e do CSV/Comparar."""

import re
from pathlib import Path

PUBLIC = Path(__file__).resolve().parent.parent / "public"
HTML = (PUBLIC / "index.html").read_text(encoding="utf-8")
JS = {n: (PUBLIC / n).read_text(encoding="utf-8") for n in ("app.js", "graficos.js", "caixa.js", "sw.js")}  # sem vendor/


def test_sem_innerhtml_nem_escrita_perigosa():
    for nome, js in JS.items():
        js = re.sub(r"(?m)^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", js, flags=re.S))   # comentários que citam a regra não contam
        for proibido in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert proibido not in js, f"{nome} usa {proibido}"


def test_html_compativel_com_a_csp_self():
    assert "<script>" not in HTML and not re.search(r"<script(?![^>]*\bsrc=)", HTML)          # nada de script inline
    assert not re.search(r"\son\w+=", HTML)                                                      # nem onclick=...
    for src in re.findall(r'<script src="([^"]+)"', HTML):
        assert src.startswith("/"), f"script externo: {src}"


def test_ids_usados_no_js_existem_no_html():
    ids = set(re.findall(r'\bid="([\w-]+)"', HTML))
    usados = set()
    for nome in ("app.js", "caixa.js"):
        usados |= set(re.findall(r'\$\("#([\w-]+)"\)', JS[nome]))
    assert usados - ids == set()


def test_abas_e_subabas_tem_secao():
    ids = set(re.findall(r'\bid="([\w-]+)"', HTML))
    for aba in re.findall(r'data-aba="(\w+)"', HTML):
        assert f"aba-{aba}" in ids
    for sub in re.findall(r'data-sub="(\w+)"', HTML):
        assert f"cx-{sub}" in ids
    assert {"aba-caixa", "aba-dados", "aba-painel", "aba-escanear", "aba-comparar", "aba-revisao"} <= ids


def test_scripts_e_service_worker_apontam_para_arquivos_que_existem():
    for src in re.findall(r'<script src="/([^"]+)"', HTML):
        assert (PUBLIC / src).exists(), src
    shell = re.search(r"const SHELL = \[(.*?)\]", JS["sw.js"], re.S).group(1)
    for caminho in re.findall(r'"/([^"]*)"', shell):
        assert caminho == "" or (PUBLIC / caminho).exists(), caminho
    for novo in ("graficos.js", "caixa.js"):
        assert f'"/{novo}"' in shell and f'src="/{novo}"' in HTML


def test_funcoes_dos_arquivos_novos_usadas_pelo_app():
    assert "carregarCaixa()" in JS["app.js"] and "sugerirLancamento(" in JS["app.js"] and "rosca(" in JS["app.js"]
    assert "function carregarCaixa" in JS["caixa.js"] and "function rosca" in JS["graficos.js"]


def test_csv_tem_categoria_e_estabelecimento(client):
    from datetime import datetime

    from smartmercado.util import TZ

    from .conftest import gerar_chave

    corpo = {"chave": gerar_chave(3), "estabelecimento": "POSTO X", "tipo_estabelecimento": "posto", "data": "2026-10-12T09:00:00", "total": 5.0,
             "origem": "foto", "itens": [{"nome": "GASOLINA", "qtd": 1, "unidade": "L", "preco_unit": 5.0, "preco_total": 5.0, "categoria": "Combustível"}]}
    assert client.post("/api/compras", json=corpo).status_code == 201
    import csv, io

    linhas = list(csv.reader(io.StringIO(client.get("/api/export.csv").content.decode("utf-8-sig")), delimiter=";"))
    cab, linha = linhas[0], dict(zip(linhas[0], linhas[1]))
    assert "estabelecimento" in cab and "categoria" in cab and "mercado" not in cab
    assert linha["estabelecimento"] == "POSTO X" and linha["tipo_estabelecimento"] == "posto" and linha["categoria"] == "Combustível"


def test_comparar_devolve_categoria(client):
    from .conftest import gerar_chave

    corpo = {"chave": gerar_chave(4), "estabelecimento": "LOJA", "data": "2026-10-12T09:00:00", "total": 5.0, "origem": "foto",
             "itens": [{"nome": "DIPIRONA 500MG", "qtd": 1, "unidade": "UN", "preco_unit": 5.0, "preco_total": 5.0, "categoria": "Saúde/Farmácia"}]}
    client.post("/api/compras", json=corpo)
    assert client.get("/api/comparar?q=dipirona").json()["produtos"][0]["categoria"] == "Saúde/Farmácia"

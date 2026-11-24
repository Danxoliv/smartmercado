import csv
import io
from datetime import datetime, timedelta

import pytest

from smartmercado import gemini, nfce
from smartmercado.util import TZ

from .conftest import CHAVE_FIXTURE, PIN, gerar_chave

DATA_FOTO = (datetime.now(TZ) - timedelta(days=3)).strftime("%Y-%m-%dT10:00:00")  # sempre recente: a foto recusa data longe de hoje

LINK = f"https://nfeweb.sefaz.go.gov.br/nfeweb/sites/nfce/danfeNFCe?p={CHAVE_FIXTURE}|2|1|1|ABC"


def nota_valida(**sobrescrever) -> dict:
    corpo = {
        "chave": CHAVE_FIXTURE,
        "mercado": "SUPERMERCADO EXEMPLO LTDA",
        "cnpj": "12345678000195",
        "data": "2026-10-09T13:45:00",
        "total": 37.98,
        "descontos": 0,
        "origem": "qrcode",
        "itens": [
            {"nome": "ARROZ TIO JOAO 5KG", "codigo": "7893500018322", "ean": "7893500018322", "qtd": 1,
             "unidade": "UN", "preco_unit": 27.99, "preco_total": 27.99, "categoria": "Mercearia"},
            {"nome": "REFRIG COCA COLA 2L", "codigo": "7894900011517", "ean": "7894900011517", "qtd": 1,
             "unidade": "UN", "preco_unit": 9.99, "preco_total": 9.99, "categoria": "Bebidas"},
        ],
    }
    corpo.update(sobrescrever)
    return corpo


# ------------------------------------------------------------- PIN

@pytest.mark.parametrize("metodo,rota", [
    ("get", "/api/ping"), ("get", "/api/painel"), ("get", "/api/comparar?q=arroz"),
    ("get", "/api/export.csv"), ("post", "/api/compras"), ("post", "/api/nfce/link"),
    ("post", "/api/nfce/foto"), ("delete", f"/api/compras/{CHAVE_FIXTURE}"), ("get", "/api/categorias"),
])
def test_todas_as_rotas_exigem_pin(anon, metodo, rota):
    assert getattr(anon, metodo)(rota).status_code == 401
    assert getattr(anon, metodo)(rota, headers={"X-App-Pin": "000000"}).status_code == 401


def test_pin_correto(anon):
    assert anon.get("/api/ping", headers={"X-App-Pin": PIN}).json() == {"ok": True}


def test_bloqueio_apos_varias_falhas(anon):
    for _ in range(5):
        assert anon.get("/api/ping", headers={"X-App-Pin": "errado"}).status_code == 401
    assert anon.get("/api/ping", headers={"X-App-Pin": PIN}).status_code == 429


def test_sem_app_pin_configurado(anon, monkeypatch):
    monkeypatch.delenv("APP_PIN")
    assert anon.get("/api/ping", headers={"X-App-Pin": ""}).status_code == 503


def test_frontend_publico(anon):
    r = anon.get("/")
    assert r.status_code == 200 and "SmartMercado" in r.text
    assert "Content-Security-Policy" in r.headers


# ------------------------------------------------------------- salvar

def test_salvar_e_duplicata(client, repo):
    r = client.post("/api/compras", json=nota_valida())
    assert r.status_code == 201 and r.json() == {"id": CHAVE_FIXTURE}
    doc = repo.docs[CHAVE_FIXTURE]
    assert isinstance(doc["data"], datetime) and doc["data"].tzinfo is not None
    assert doc["data"].utcoffset().total_seconds() == -3 * 3600
    assert isinstance(doc["total"], float) and isinstance(doc["itens"][0]["qtd"], float)

    r2 = client.post("/api/compras", json=nota_valida(mercado="Outro"))
    assert r2.status_code == 409
    assert repo.docs[CHAVE_FIXTURE]["mercado"] == "SUPERMERCADO EXEMPLO LTDA"


def test_chave_com_espacos_e_normalizada(client, repo):
    chave_fmt = " ".join(CHAVE_FIXTURE[i:i + 4] for i in range(0, 44, 4))
    assert client.post("/api/compras", json=nota_valida(chave=chave_fmt)).status_code == 201
    assert CHAVE_FIXTURE in repo.docs


@pytest.mark.parametrize("campo,valor", [
    ("chave", "123"),
    ("chave", CHAVE_FIXTURE[:-1] + "9"),        # DV errado
    ("mercado", ""),
    ("data", "ontem"),
    ("total", -1),
    ("total", "abc"),
    ("origem", "manual"),
    ("itens", []),
    ("cnpj", "123"),
    ("extra", "campo desconhecido"),
])
def test_validacao_pydantic(client, repo, campo, valor):
    assert client.post("/api/compras", json=nota_valida(**{campo: valor})).status_code == 422
    assert repo.docs == {}


@pytest.mark.parametrize("item_ruim", [
    {"categoria": "Inexistente"},
    {"qtd": 0},
    {"preco_total": -5},
    {"nome": "   "},
    {"ean": "7893500018323"},
    {"html": "<script>"},
])
def test_validacao_itens(client, item_ruim):
    corpo = nota_valida()
    corpo["itens"][0].update(item_ruim)
    assert client.post("/api/compras", json=corpo).status_code == 422


def test_excluir(client, repo):
    client.post("/api/compras", json=nota_valida())
    assert client.delete(f"/api/compras/{CHAVE_FIXTURE}").status_code == 200
    assert repo.docs == {}
    assert client.delete(f"/api/compras/{CHAVE_FIXTURE}").status_code == 404
    assert client.delete("/api/compras/abc").status_code == 422


# ------------------------------------------------------------- leitura

def test_ler_link(client, monkeypatch, html_sintetico):
    monkeypatch.setattr(nfce, "baixar_pagina", lambda url: html_sintetico)
    r = client.post("/api/nfce/link", json={"url": LINK})
    assert r.status_code == 200
    d = r.json()
    assert d["origem"] == "qrcode" and d["chave"] == CHAVE_FIXTURE and d["total"] == 50.36
    assert d["soma_confere"] is True and len(d["itens"]) == 4


def test_ler_link_recusa_host(client, monkeypatch):
    monkeypatch.setattr(nfce, "baixar_pagina", lambda url: pytest.fail("não deveria baixar"))
    r = client.post("/api/nfce/link", json={"url": "https://evil.com/nfeweb?p=1"})
    assert r.status_code == 400


def test_ler_link_sefaz_fora(client, monkeypatch):
    def falha(url):
        raise nfce.ErroSefaz("Falha ao acessar a SEFAZ: ConnectTimeout")
    monkeypatch.setattr(nfce, "baixar_pagina", falha)
    assert client.post("/api/nfce/link", json={"url": LINK}).status_code == 502


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100


def test_ler_foto(client, monkeypatch):
    def falso(dados, mime):
        assert mime == "image/jpeg"
        return gemini.normalizar_resposta({
            "mercado": "MERCADO FOTO", "cnpj": None, "data_emissao": DATA_FOTO,
            "chave_acesso": CHAVE_FIXTURE, "valor_a_pagar": 20.0,
            "itens": [{"nome": "DET YPE", "quantidade": 2, "preco_unitario": 2.5, "preco_total": 5.0},
                      {"nome": "ARROZ 5KG", "codigo": "7893500018322", "quantidade": 1,
                       "preco_unitario": 12.0, "preco_total": 12.0}],
        })
    monkeypatch.setattr(gemini, "ler_foto", falso)
    r = client.post("/api/nfce/foto", files={"foto": ("n.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 200
    d = r.json()
    assert d["origem"] == "foto" and d["cnpj"] == "12345678000195"  # CNPJ tirado da chave
    assert d["itens"][1]["ean"] == "7893500018322" and d["itens"][0]["categoria"] == "Limpeza"
    assert d["soma_itens"] == 17.0 and d["soma_confere"] is False and d["diferenca"] == -3.0
    assert d["data"] == DATA_FOTO + "-03:00"


def test_foto_tipo_invalido(client):
    r = client.post("/api/nfce/foto", files={"foto": ("x.jpg", b"%PDF-1.4 ...", "image/jpeg")})
    assert r.status_code == 415


def test_foto_grande_demais(client):
    r = client.post("/api/nfce/foto", files={"foto": ("x.jpg", JPEG + b"0" * gemini.MAX_IMAGEM, "image/jpeg")})
    assert r.status_code == 413


def test_gemini_usa_json_e_schema(monkeypatch):
    """Garante response_mime_type JSON + response_schema na chamada ao SDK."""
    from google import genai

    chamadas = {}

    class Resp:
        text = '{"mercado": "X", "valor_a_pagar": 5, "itens": [{"nome": "SAL", "quantidade": 1, "preco_unitario": 5, "preco_total": 5}]}'

    class Modelos:
        def generate_content(self, model, contents, config):
            chamadas.update(model=model, config=config)
            return Resp()

    class Cliente:
        def __init__(self, api_key, http_options=None):
            self.models = Modelos()

    monkeypatch.setenv("GEMINI_API_KEY", "teste")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(genai, "Client", Cliente)
    nota = gemini.ler_foto(JPEG, "image/jpeg")
    assert chamadas["model"] == "gemini-3.8-flash"
    assert chamadas["config"].response_mime_type == "application/json"
    assert chamadas["config"].response_schema is not None
    assert nota["itens"][0]["categoria"] == "Mercearia" and nota["total"] == 5


# ------------------------------------------------------------- painel / comparador / CSV

def _popular(client):
    dados = [
        (1, "MERCADO A", "11111111000111", "2026-10-02T10:00:00", [("ARROZ TIO JOAO 5KG", "7893500018322", 27.99, "Mercearia"), ("DET YPE 500ML", None, 2.49, "Limpeza")]),
        (2, "MERCADO B", "22222222000122", "2026-10-05T18:30:00", [("ARROZ T JOAO 5 KG", "7893500018322", 25.49, "Mercearia")]),
        (3, "MERCADO A", "11111111000111", "2026-10-20T09:00:00", [("ARROZ TIO JOAO 5KG", "7893500018322", 26.90, "Mercearia"), ("Det. Ypê 500ml", None, 2.79, "Limpeza")]),
        (4, "MERCADO B", "22222222000122", "2026-09-28T09:00:00", [("ARROZ TIO JOAO 5KG", "7893500018322", 24.00, "Mercearia")]),
    ]
    for n, mercado, cnpj, data, itens in dados:
        corpo = nota_valida(
            chave=gerar_chave(n, cnpj), mercado=mercado, cnpj=cnpj, data=data,
            total=round(sum(p for _, _, p, _ in itens), 2),
            itens=[{"nome": nome, "codigo": ean, "ean": ean, "qtd": 1, "unidade": "UN",
                    "preco_unit": p, "preco_total": p, "categoria": c} for nome, ean, p, c in itens],
        )
        assert client.post("/api/compras", json=corpo).status_code == 201


def test_painel_mes(client):
    _popular(client)
    p = client.get("/api/painel?mes=2026-10").json()
    assert p["mes"] == "2026-10"
    assert p["qtd_compras"] == 3
    assert p["total"] == round(27.99 + 2.49 + 25.49 + 26.90 + 2.79, 2)
    cats = {c["categoria"]: c["total"] for c in p["por_categoria"]}
    assert cats == {"Mercearia": 80.38, "Limpeza": 5.28}
    mercados = {m["mercado"]: (m["total"], m["compras"]) for m in p["por_mercado"]}
    assert mercados == {"MERCADO A": (60.17, 2), "MERCADO B": (25.49, 1)}
    assert [c["mercado"] for c in p["compras"]] == ["MERCADO A", "MERCADO B", "MERCADO A"]
    assert client.get("/api/painel?mes=2026-09").json()["qtd_compras"] == 1
    assert client.get("/api/painel?mes=2026-13").status_code == 422


def test_painel_distribui_desconto(client):
    corpo = nota_valida(total=34.0, descontos=3.98)  # itens somam 37,98
    client.post("/api/compras", json=corpo)
    p = client.get("/api/painel?mes=2026-10").json()
    assert round(sum(c["total"] for c in p["por_categoria"]), 2) == 34.0


def test_comparar_agrupa_por_ean(client):
    _popular(client)
    produtos = client.get("/api/comparar?q=arroz").json()["produtos"]
    assert len(produtos) == 1  # nomes diferentes, mesmo EAN
    arroz = produtos[0]
    assert arroz["ean"] == "7893500018322" and len(arroz["historico"]) == 4
    assert arroz["nome"] == "ARROZ TIO JOAO 5KG"  # nome da compra mais recente
    pm = {m["mercado"]: m for m in arroz["por_mercado"]}
    assert pm["MERCADO A"]["ultimo_preco"] == 26.90 and pm["MERCADO A"]["menor_preco"] == 26.90
    assert pm["MERCADO B"]["ultimo_preco"] == 25.49 and pm["MERCADO B"]["menor_preco"] == 24.00
    assert arroz["por_mercado"][0]["mercado"] == "MERCADO B"  # mais barato primeiro
    assert arroz["historico"][0]["data"].startswith("2026-10-20")


def test_comparar_agrupa_por_nome_normalizado(client):
    _popular(client)
    produtos = client.get("/api/comparar?q=ype").json()["produtos"]
    assert len(produtos) == 1  # "DET YPE 500ML" e "Det. Ypê 500ml"
    assert len(produtos[0]["historico"]) == 2


def test_comparar_por_ean_digitado(client):
    _popular(client)
    assert len(client.get("/api/comparar?q=7893500018322").json()["produtos"]) == 1


def test_comparar_busca_curta(client):
    assert client.get("/api/comparar?q=a").status_code == 422


def test_export_csv(client):
    _popular(client)
    r = client.get("/api/export.csv?mes=2026-10")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="smartmercado-2026-10.csv"' in r.headers["content-disposition"]
    texto = r.content.decode("utf-8-sig")
    linhas = list(csv.reader(io.StringIO(texto), delimiter=";"))
    assert linhas[0][:3] == ["data", "hora", "estabelecimento"]
    assert len(linhas) == 1 + 5  # cabeçalho + 5 itens de outubro
    primeira = dict(zip(linhas[0], linhas[1]))
    assert primeira["data"] == "02/10/2026" and primeira["preco_total"] == "27,99"
    assert primeira["cnpj"] == "11.111.111/0001-11"
    assert len(list(csv.reader(io.StringIO(client.get("/api/export.csv").content.decode("utf-8-sig")), delimiter=";"))) == 1 + 6


def test_csv_neutraliza_formulas(client):
    corpo = nota_valida()
    corpo["itens"][0]["nome"] = "=HYPERLINK(\"http://x\")"
    client.post("/api/compras", json=corpo)
    texto = client.get("/api/export.csv").content.decode("utf-8-sig")
    assert "'=HYPERLINK" in texto


def test_data_salva_no_fuso_de_brasilia(client, repo):
    client.post("/api/compras", json=nota_valida(data="2026-10-31T23:30:00"))
    assert repo.docs[CHAVE_FIXTURE]["data"] == datetime(2026, 10, 31, 23, 30, tzinfo=TZ)
    assert client.get("/api/painel?mes=2026-10").json()["qtd_compras"] == 1
    assert client.get("/api/painel?mes=2026-11").json()["qtd_compras"] == 0


# ------------------------------------------------------------- cupom sem chave fiscal / edição

def test_cupom_sem_chave_gera_id_e_nao_duplica(client, repo):
    corpo = nota_valida(chave="", cnpj="00000000000191", origem="foto")
    r = client.post("/api/compras", json=corpo)
    assert r.status_code == 201
    chave = r.json()["id"]
    assert len(chave) == 44 and chave.startswith("00") and set(chave) != {"0"}
    assert repo.docs[chave]["chave"] == chave
    assert client.post("/api/compras", json=corpo).status_code == 409           # mesmo cupom, mesmo dia
    assert client.post("/api/compras", json=nota_valida(chave="0" * 44, origem="foto", total=1.0)).status_code == 201  # zeros = sem chave


def test_gemini_descarta_chave_inventada():
    n = gemini.normalizar_resposta({"mercado": "B12", "chave_acesso": "0" * 44, "valor_a_pagar": 5,
                                    "itens": [{"nome": "SAL", "quantidade": 1, "preco_unitario": 5, "preco_total": 5}]})
    assert n["chave"] == "" and n["data"] is None


def test_listar_compras_em_horario_de_brasilia(client):
    _popular(client)
    todas = client.get("/api/compras").json()["compras"]
    assert len(todas) == 4 and "criado_em" not in todas[0]
    assert todas[0]["data"].startswith("2026-10-20T09:00:00-03:00")
    assert len(client.get("/api/compras?mes=2026-10").json()["compras"]) == 3


def test_alterar_compra(client, repo):
    client.post("/api/compras", json=nota_valida())
    novo = nota_valida(total=40.0, mercado="MERCADO NOVO")
    novo["itens"][0].update(preco_unit=30.01, preco_total=30.01)
    assert client.put(f"/api/compras/{CHAVE_FIXTURE}", json=novo).json() == {"id": CHAVE_FIXTURE}
    assert repo.docs[CHAVE_FIXTURE]["mercado"] == "MERCADO NOVO"
    assert repo.docs[CHAVE_FIXTURE]["itens"][0]["preco_total"] == 30.01


def test_alterar_compra_erros(client):
    outra = gerar_chave(7)
    assert client.put(f"/api/compras/{outra}", json=nota_valida(chave=outra)).status_code == 404
    client.post("/api/compras", json=nota_valida())
    assert client.put(f"/api/compras/{CHAVE_FIXTURE}", json=nota_valida(chave=outra)).status_code == 422  # chave do corpo ≠ URL
    assert client.put("/api/compras/123", json=nota_valida()).status_code == 422


def test_gemini_cai_para_modelo_reserva(monkeypatch):
    from google import genai

    usados = []

    class Resp:
        text = '{"mercado": "X", "valor_a_pagar": 5, "itens": [{"nome": "SAL", "quantidade": 1, "preco_unitario": 5, "preco_total": 5}]}'

    class Modelos:
        def generate_content(self, model, contents, config):
            usados.append(model)
            if model != "gemini-3.6-flash":
                raise RuntimeError("503 UNAVAILABLE. high demand")
            return Resp()

    class Cliente:
        def __init__(self, api_key, http_options=None):
            self.models = Modelos()

    monkeypatch.setenv("GEMINI_API_KEY", "teste")
    monkeypatch.setattr(genai, "Client", Cliente)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    assert gemini.ler_foto(JPEG, "image/jpeg")["total"] == 5
    assert usados == ["gemini-3.8-flash"] * 2 + ["gemini-3.7-flash"] * 2 + ["gemini-3.6-flash"]


def test_gemini_todos_ocupados_mensagem_clara(monkeypatch, client):
    monkeypatch.setattr(gemini, "ler_foto", lambda d, m: (_ for _ in ()).throw(gemini.ErroGemini("O Gemini está sobrecarregado agora.")))
    r = client.post("/api/nfce/foto", files={"foto": ("n.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 502 and "sobrecarregado" in r.json()["detail"]


def test_comparativo(client):
    _popular(client)
    d = client.get("/api/comparativo?mes=2026-10").json()
    assert [m["mes"] for m in d["meses"]] == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert d["atual"] == 85.66 and d["anterior"] == 24.0 and d["variacao_pct"] == 256.9
    assert d["meses"][-1]["compras"] == 3 and d["meses"][0]["total"] == 0 and d["ticket_medio"] == 28.55
    cats = {c["categoria"]: (c["atual"], c["anterior"]) for c in d["categorias"]}
    assert cats == {"Mercearia": (80.38, 24.0), "Limpeza": (5.28, 0.0)}
    assert client.get("/api/comparativo?mes=2026-13").status_code == 422


def test_comparativo_virada_de_ano_e_sem_dados(client):
    d = client.get("/api/comparativo?mes=2026-02").json()
    assert [m["mes"] for m in d["meses"]] == ["2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"]
    assert d["variacao_pct"] is None and d["atual"] == 0 and d["categorias"] == []


def test_gemini_504_e_429_passam_para_o_proximo_modelo_com_prazo_por_chamada(monkeypatch):
    from google import genai

    usados, http = [], []

    class Resp:
        text = '{"estabelecimento": "X", "valor_a_pagar": 5, "itens": [{"nome": "SAL", "quantidade": 1, "preco_unitario": 5, "preco_total": 5}]}'

    class Modelos:
        def generate_content(self, model, contents, config):
            usados.append(model)
            if model == "gemini-3.8-flash":
                raise RuntimeError("429 RESOURCE_EXHAUSTED. cota")
            if model == "gemini-3.7-flash":
                raise RuntimeError("504 DEADLINE_EXCEEDED. Deadline expired")
            return Resp()

    class Cliente:
        def __init__(self, api_key, http_options=None):
            http.append(http_options)
            self.models = Modelos()

    monkeypatch.setenv("GEMINI_API_KEY", "teste")
    monkeypatch.setattr(genai, "Client", Cliente)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: pytest.fail("não deveria esperar: 429/504 vão direto ao próximo modelo"))
    assert gemini.ler_foto(JPEG, "image/jpeg")["total"] == 5
    assert usados == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"] and http[0].timeout == gemini.TIMEOUT_CHAMADA_MS


def test_gemini_todos_com_timeout_da_mensagem_clara(monkeypatch):
    from google import genai

    class Cliente:
        def __init__(self, api_key, http_options=None):
            self.models = type("M", (), {"generate_content": lambda s, **k: (_ for _ in ()).throw(RuntimeError("504 DEADLINE_EXCEEDED"))})()

    monkeypatch.setenv("GEMINI_API_KEY", "teste")
    monkeypatch.setattr(genai, "Client", Cliente)
    with pytest.raises(gemini.ErroGemini, match="demorou demais"):
        gemini.ler_foto(JPEG, "image/jpeg")


def test_cupom_com_desconto_ja_embutido_na_linha_confere():
    """Farmácia: o Vl. Total do item já é líquido (20,00 - 4,00 = 16,00) e 'valor a pagar' = soma das linhas."""
    from smartmercado.models import conferir_soma

    n = gemini.normalizar_resposta({"estabelecimento": "FARMACIA EXEMPLO LTDA", "cnpj": "11222333000181", "valor_a_pagar": 38.49, "descontos": 4.00,
        "itens": [{"nome": "DIPIRONA 500MG C/ 10 CP", "quantidade": 1, "preco_unitario": 20.00, "preco_total": 16.00},
                  {"nome": "TOALHA UMEDECIDA 50UN", "quantidade": 1, "preco_unitario": 9.99, "preco_total": 9.99},
                  {"nome": "CREME DENTAL XYZ 90G", "quantidade": 1, "preco_unitario": 12.50, "preco_total": 12.50}]})
    n = conferir_soma(n)
    assert n["soma_itens"] == 38.49 and n["soma_confere"] is True and n["descontos"] == 0.0 and n["diferenca"] == 0
    assert n["itens"][2]["categoria"] == "Higiene"


def test_desconto_separado_continua_somando_certo():
    from smartmercado.models import conferir_soma

    n = conferir_soma({"total": 50.36, "descontos": 1.0, "itens": [{"preco_total": 51.36}]})
    assert n["soma_confere"] is True and n["descontos"] == 1.0
    assert conferir_soma({"total": 40.0, "descontos": 1.0, "itens": [{"preco_total": 51.36}]})["soma_confere"] is False


def test_foto_cnpj_mal_lido_e_data_com_ano_errado_sao_descartados():
    base = {"estabelecimento": "FARMACIA EXEMPLO", "valor_a_pagar": 5, "itens": [{"nome": "SAL", "quantidade": 1, "preco_unitario": 5, "preco_total": 5}]}
    ok = gemini.normalizar_resposta({**base, "cnpj": "11.222.333/0001-81", "data_emissao": DATA_FOTO})
    assert ok["cnpj"] == "11222333000181" and ok["data"] == DATA_FOTO + "-03:00"
    ruim = gemini.normalizar_resposta({**base, "cnpj": "11222833000181", "data_emissao": "2024-10-16T13:30:21"})   # 3 lido como 8; 2026 lido como 2024
    assert ruim["cnpj"] == "" and ruim["data"] is None
    futuro = gemini.normalizar_resposta({**base, "data_emissao": "2031-01-01T10:00:00"})
    assert futuro["data"] is None
    # CNPJ inválido, mas a chave de acesso é válida: o CNPJ vem da chave
    k = gemini.normalizar_resposta({**base, "cnpj": "11111111111110", "chave_acesso": CHAVE_FIXTURE})
    assert k["cnpj"] == "12345678000195"


def test_cnpj_valido():
    from smartmercado.util import cnpj_valido

    assert all(cnpj_valido(c) for c in ("11222333000181", "12345678000195", "00000000000191"))
    assert not any(cnpj_valido(c) for c in ("11222833000181", "00000000000000", "123", "", None, "1234567800019A"))

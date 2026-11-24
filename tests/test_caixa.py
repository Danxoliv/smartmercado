"""Fase 3: caixa (lançamentos, recorrência, parcelas, projeção por dia, rotas /api/caixa/*)."""

import json
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from smartmercado import caixa
from smartmercado.util import TZ

from .conftest import CHAVE_FIXTURE, FIXTURES, gerar_chave

REL = json.loads((FIXTURES / "caixa_exemplo.json").read_text(encoding="utf-8"))


def lanc(**k):
    return {"tipo": "saida", "nome": "X", "valor": 10.0, "data": date(2026, 10, 5), "recorrencia": "unica", **k}


def datas(l, ini, fim):
    return [(d.isoformat(), n) for d, n in caixa.ocorrencias(l, ini, fim)]


# ------------------------------------------------------------- expansão

def test_unica_dentro_e_fora():
    assert datas(lanc(), date(2026, 10, 1), date(2026, 10, 31)) == [("2026-10-05", None)]
    assert datas(lanc(), date(2026, 10, 6), date(2026, 10, 31)) == []


def test_mensal_dia_31_vira_ultimo_dia():
    l = lanc(data=date(2027, 1, 31), recorrencia="mensal")
    assert [d for d, _ in datas(l, date(2027, 1, 1), date(2027, 4, 30))] == ["2027-01-31", "2027-02-28", "2027-03-31", "2027-04-30"]


def test_anual_29_de_fevereiro():
    l = lanc(data=date(2028, 2, 29), recorrencia="anual")
    assert [d for d, _ in datas(l, date(2028, 1, 1), date(2030, 12, 31))] == ["2028-02-29", "2029-02-28", "2030-02-28"]


def test_parcelas_comecam_na_parcela_atual_e_terminam_no_total():
    l = lanc(data=date(2026, 10, 5), recorrencia="mensal", parcela_atual=2, parcelas_total=4)
    assert datas(l, date(2026, 1, 1), date(2027, 12, 31)) == [("2026-10-05", 2), ("2026-11-05", 3), ("2026-12-05", 4)]
    # janela começando depois: a contagem continua certa
    assert datas(l, date(2026, 11, 1), date(2027, 12, 31)) == [("2026-11-05", 3), ("2026-12-05", 4)]


def test_parcela_de_44_vezes_termina_em_maio_2030():
    l = lanc(data=date(2026, 10, 3), recorrencia="mensal", parcela_atual=1, parcelas_total=44)
    todas = datas(l, date(2026, 10, 1), date(2035, 1, 1))
    assert len(todas) == 44 and todas[-1] == ("2030-05-03", 44)


def test_diaria_e_semanal():
    assert [d for d, _ in datas(lanc(data=date(2026, 10, 30), recorrencia="diaria"), date(2026, 10, 1), date(2026, 11, 2))] == \
        ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]
    assert [d for d, _ in datas(lanc(data=date(2026, 9, 28), recorrencia="semanal"), date(2026, 10, 1), date(2026, 10, 31))] == \
        ["2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"]


# ------------------------------------------------------------- projeção (conta feita à mão)

def _cenario():
    return [
        lanc(tipo="entrada", nome="Salário", valor=1000, data=date(2026, 11, 5), recorrencia="mensal"),
        lanc(tipo="saida", nome="Aluguel", valor=300, data=date(2026, 11, 1), recorrencia="mensal"),
        lanc(tipo="diario", nome="Diário", valor=10, data=date(2026, 11, 1), recorrencia="diaria"),
        lanc(tipo="economia", nome="Poupança", valor=50, data=date(2026, 11, 10), recorrencia="mensal"),
        lanc(tipo="cartao", nome="Cartão", valor=200, data=date(2026, 11, 15), recorrencia="mensal"),
    ]


def test_projetar_um_mes():
    p = caixa.projetar(_cenario(), date(2026, 11, 1), meses=1, saldo_inicial=100)
    m = p["meses"][0]
    assert (m["entradas"], m["saidas"], m["diarios"], m["economias"], m["cartao"]) == (1000, 300, 300, 50, 200)
    assert m["performance"] == 150 == 1000 - 300 - 300 - 50 - 200                # entradas - saídas - diários - economias - cartão
    assert m["custo_vida"] == 800 == 300 + 300 + 200                              # saídas + diários + cartão (sem economia)
    assert m["saldo_final"] == 250 == p["saldo_final"] == 100 + 150
    assert (m["saldo_minimo"], m["dia_minimo"]) == (-240, "2026-11-04")           # antes do salário do dia 5
    assert len(p["dias"]) == 30 and p["dias"][0] == {"data": "2026-11-01", "saldo": -210.0} and p["dias"][-1]["saldo"] == 250


def test_projetar_12_meses_por_padrao_e_virada_de_ano():
    p = caixa.projetar(_cenario(), date(2026, 11, 1))
    assert [m["mes"] for m in p["meses"]][:3] == ["2026-11", "2026-12", "2027-01"] and p["meses"][-1]["mes"] == "2027-10"
    assert p["fim"] == "2027-10-31" and len(p["dias"]) == 365
    assert p["performance_total"] == round(sum(m["performance"] for m in p["meses"]), 2)


def test_projetar_comeco_no_meio_do_mes_e_sem_lancamentos():
    p = caixa.projetar(_cenario(), date(2026, 11, 20), meses=1)
    assert p["meses"][0]["entradas"] == 0 and p["meses"][0]["diarios"] == 110     # só 11 dias, sem salário (dia 5 já passou)
    vazio = caixa.projetar([], date(2026, 1, 1), meses=2, saldo_inicial=5)
    assert vazio["saldo_final"] == 5 and vazio["performance_total"] == 0


def test_centavos_sem_erro_de_ponto_flutuante():
    ls = [lanc(tipo="entrada", valor=0.1, data=date(2026, 1, 1), recorrencia="diaria")]
    assert caixa.projetar(ls, date(2026, 1, 1), meses=1)["saldo_final"] == 3.1     # 31 x 0,10


# ------------------------------------------------------------- conjunto de exemplo (fictício) conferido mês a mês

def _projetar_exemplo():
    ls = [caixa.de_doc(caixa.para_doc(caixa.LancamentoIn(**l))) for l in REL["lancamentos"]]
    return caixa.projetar(ls, date.fromisoformat(REL["inicio"]), meses=12, saldo_inicial=REL["saldo_inicial"])


def test_exemplo_mes_a_mes():
    p = _projetar_exemplo()
    assert [m["mes"] for m in p["meses"]] == REL["meses"]
    for i, m in enumerate(p["meses"]):
        assert (m["entradas"], m["saidas"], m["diarios"], m["economias"], m["cartao"]) == pytest.approx(
            (REL["ent"][i], REL["sai"][i], REL["dia"][i], REL["eco"][i], REL["car"][i]), abs=0.005), m["mes"]
        assert m["saldo_final"] == pytest.approx(REL["fin"][i], abs=0.005), m["mes"]


def test_exemplo_numeros_conferidos():
    p = _projetar_exemplo()
    m = {x["mes"]: x for x in p["meses"]}
    assert m["2026-10"]["saldo_final"] == 905.5 and m["2026-11"]["saldo_final"] == 2276.0
    assert m["2027-03"]["saldo_final"] == 4044.0 and m["2027-09"]["saldo_final"] == 8745.0
    assert m["2026-10"]["performance"] == 405.5 and m["2027-09"]["performance"] == 801.0
    assert p["performance_total"] == 8245.0 and p["saldo_inicial"] == 500.0
    assert round(p["saldo_final"] - p["saldo_inicial"], 2) == 8245.0


def test_exemplo_parcelas():
    ls = [caixa.de_doc(caixa.para_doc(caixa.LancamentoIn(**l))) for l in REL["lancamentos"]]
    por_nome = {p["nome"]: p for p in caixa.resumo_parcelas(ls, date(2026, 11, 10))}
    carro = por_nome["Parcela do carro"]
    assert (carro["parcelas_total"], carro["parcela_atual"], carro["restantes"]) == (36, 2, 34)
    assert carro["termina_em"] == "2029-09-03" and carro["proxima"] == "2026-12-03" and carro["total_restante"] == 18360.0
    assert por_nome["Compra 6x"]["termina_em"] == "2027-03-12" and por_nome["Compra 4x"]["termina_em"] == "2027-01-12"
    assert por_nome["Compra 2x"]["termina_em"] == "2026-12-12" and por_nome["Compra 2x"]["restantes"] == 2 and por_nome["Compra 2x"]["parcela_atual"] is None
    assert por_nome["Compra 5x"]["termina_em"] == "2027-02-12"


# ------------------------------------------------------------- validação

@pytest.mark.parametrize("extra", [
    {"valor": 0}, {"valor": -5}, {"tipo": "outro"}, {"nome": "  "}, {"data": "ontem"}, {"chave_nota": "123"},
    {"parcela_atual": 2},                                                         # sem parcelas_total
    {"parcelas_total": 3, "parcela_atual": 4},                                    # atual > total
    {"parcelas_total": 3, "recorrencia": "diaria"},                               # parcela precisa ser mensal
    {"campo_desconhecido": 1},
])
def test_lancamento_invalido(extra):
    base = {"tipo": "saida", "nome": "X", "valor": 10, "data": "2026-10-05"}
    with pytest.raises(ValidationError):
        caixa.LancamentoIn(**{**base, **extra})


def test_parcelado_vira_mensal_e_comeca_na_parcela_1():
    l = caixa.LancamentoIn(tipo="cartao", nome="TV", valor=100, data="2026-10-05", parcelas_total=10)
    assert l.recorrencia == "mensal" and l.parcela_atual == 1


def test_id_deterministico():
    a = caixa.LancamentoIn(tipo="saida", nome="Aluguel ", valor=720, data="2026-10-01")
    b = caixa.LancamentoIn(tipo="saida", nome="ALUGUEL", valor=720.0, data="2026-10-01")
    c = caixa.LancamentoIn(tipo="saida", nome="Aluguel", valor=720, data="2026-11-01")
    assert caixa.id_lancamento(a) == caixa.id_lancamento(b) != caixa.id_lancamento(c)
    p1 = caixa.LancamentoIn(tipo="cartao", nome="TV", valor=100, data="2026-10-05", parcelas_total=10, parcela_atual=1)
    p2 = caixa.LancamentoIn(tipo="cartao", nome="TV", valor=100, data="2026-10-05", parcelas_total=10, parcela_atual=2)
    assert caixa.id_lancamento(p1) != caixa.id_lancamento(p2)


# ------------------------------------------------------------- rotas

ROTAS = [
    ("get", "/api/caixa/lancamentos"), ("post", "/api/caixa/lancamentos"), ("put", "/api/caixa/lancamentos/abc"),
    ("delete", "/api/caixa/lancamentos/abc"), ("get", "/api/caixa/projecao"), ("get", "/api/caixa/parcelas"),
    ("get", "/api/caixa/config"), ("put", "/api/caixa/config"), ("get", f"/api/caixa/sugestao/{CHAVE_FIXTURE}"),
]


@pytest.mark.parametrize("metodo,rota", ROTAS)
def test_rotas_do_caixa_exigem_pin(anon, metodo, rota):
    assert getattr(anon, metodo)(rota).status_code == 401


def test_crud(client, repo):
    corpo = {"tipo": "saida", "nome": "Dentista", "valor": 250, "data": "2026-10-20"}
    r = client.post("/api/caixa/lancamentos", json=corpo)
    assert r.status_code == 201
    id = r.json()["id"]
    assert repo.colecoes["lancamentos"][id]["data"] == "2026-10-20" and repo.colecoes["lancamentos"][id]["origem"] == "manual"
    assert client.get("/api/caixa/lancamentos").json()["lancamentos"][0]["nome"] == "Dentista"
    assert client.put(f"/api/caixa/lancamentos/{id}", json={**corpo, "valor": 300}).status_code == 200
    assert repo.colecoes["lancamentos"][id]["valor"] == 300
    assert client.put("/api/caixa/lancamentos/naoexiste", json=corpo).status_code == 404
    assert client.put("/api/caixa/lancamentos/a.b", json=corpo).status_code == 422
    assert client.delete(f"/api/caixa/lancamentos/{id}").status_code == 200
    assert client.delete(f"/api/caixa/lancamentos/{id}").status_code == 404
    assert client.post("/api/caixa/lancamentos", json={**corpo, "valor": -1}).status_code == 422


def test_lancamento_manual_repetido_nao_colide_mas_importado_e_idempotente(client, repo):
    base = {"tipo": "diario", "nome": "Café", "valor": 6, "data": "2026-10-20"}
    assert client.post("/api/caixa/lancamentos", json=base).json()["id"] != client.post("/api/caixa/lancamentos", json=base).json()["id"]
    importado = {**base, "origem": "importado"}
    a, b = client.post("/api/caixa/lancamentos", json=importado).json()["id"], client.post("/api/caixa/lancamentos", json=importado).json()["id"]
    assert a == b and sum(1 for d in repo.colecoes["lancamentos"].values() if d["origem"] == "importado") == 1


def test_projecao_pela_api_com_dados_do_relatorio(client):
    for l in REL["lancamentos"]:
        assert client.post("/api/caixa/lancamentos", json=l).status_code == 201
    assert client.put("/api/caixa/config", json={"saldo_inicial": REL["saldo_inicial"], "data_saldo": REL["inicio"]}).status_code == 200
    p = client.get("/api/caixa/projecao").json()
    assert [m["saldo_final"] for m in p["meses"]] == pytest.approx(REL["fin"], abs=0.005)
    assert p["performance_total"] == 8245.0
    curta = client.get("/api/caixa/projecao?inicio=2026-11-01&meses=2&saldo_inicial=0").json()
    assert len(curta["meses"]) == 2 and curta["saldo_inicial"] == 0
    assert client.get("/api/caixa/projecao?meses=0").status_code == 422
    assert client.get("/api/caixa/projecao?meses=61").status_code == 422


def test_config_padrao_e_validacao(client):
    assert client.get("/api/caixa/config").json() == {"saldo_inicial": 0.0, "data_saldo": None}
    assert client.put("/api/caixa/config", json={"saldo_inicial": "abc", "data_saldo": "2026-10-01"}).status_code == 422


def test_sugestao_de_lancamento_para_nota_salva_e_um_lancamento_por_nota(client, repo):
    repo.docs[CHAVE_FIXTURE] = {"chave": CHAVE_FIXTURE, "estabelecimento": "MERCADINHO X", "mercado": "MERCADINHO X",
                                "data": datetime(2026, 10, 9, 16, 37, tzinfo=TZ), "total": 210.63, "itens": []}
    s = client.get(f"/api/caixa/sugestao/{CHAVE_FIXTURE}").json()
    assert s == {"tipo": "diario", "nome": "MERCADINHO X", "valor": 210.63, "data": "2026-10-09", "recorrencia": "unica",
                 "origem": "nota", "chave_nota": CHAVE_FIXTURE}
    assert client.post("/api/caixa/lancamentos", json={**s, "tipo": "cartao"}).status_code == 201
    assert f"nota-{CHAVE_FIXTURE}" in repo.colecoes["lancamentos"]
    assert client.post("/api/caixa/lancamentos", json=s).status_code == 409           # a mesma nota não lança duas vezes
    assert client.get("/api/caixa/sugestao/" + "5" * 44).status_code == 422             # 44 dígitos, mas sem DV válido
    assert client.get("/api/caixa/sugestao/" + gerar_chave(77)).status_code == 404       # chave válida, nota inexistente
    assert client.get("/api/caixa/sugestao/123").status_code == 422


def test_parcelas_pela_api(client):
    client.post("/api/caixa/lancamentos", json={"tipo": "cartao", "nome": "TV", "valor": 100, "data": "2020-01-10", "parcelas_total": 12})
    p = client.get("/api/caixa/parcelas").json()["parcelas"]
    assert p[0]["restantes"] == 0 and p[0]["termina_em"] == "2020-12-10" and p[0]["total_restante"] == 0

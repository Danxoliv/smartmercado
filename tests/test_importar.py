"""Fase 4: importação idempotente de lançamentos (JSON/CSV)."""

import json

import pytest

from smartmercado import caixa, importar

from .conftest import FIXTURES, RepoMemoria

REL = json.loads((FIXTURES / "caixa_exemplo.json").read_text(encoding="utf-8"))
JSON_OBJ = json.dumps({
    "_comentario": "ignorado",
    "config": {"saldo_inicial": 500.0, "data_saldo": "2026-10-01"},
    "lancamentos": [
        {"_x": 1, "tipo": "entrada", "nome": "Salário", "valor": 2500, "data": "2026-10-05", "recorrencia": "mensal"},
        {"tipo": "cartao", "nome": "TV", "valor": 400, "data": "2026-10-12", "parcela_atual": 1, "parcelas_total": 6},
    ],
}).encode()
CSV_PT = (
    "Tipo;Nome;Valor;Data;Recorrencia;Parcela_atual;Parcelas_total\n"
    "entrada;Salário;2.500,00;05/10/2026;mensal;;\n"
    "cartao;TV;400,00;12/10/2026;;1;6\n"
    "\n"
    "saida;Aluguel;600;2026-10-01;mensal;;\n"
).encode()


def lidos(dados):
    itens, cfg = importar.ler(dados)
    return itens, cfg


# ------------------------------------------------------------- leitura

def test_json_objeto_com_config():
    itens, cfg = lidos(JSON_OBJ)
    assert [i.nome for i in itens] == ["Salário", "TV"] and all(i.origem == "importado" for i in itens)
    assert itens[1].recorrencia == "mensal" and itens[1].parcelas_total == 6                # parcelado vira mensal
    assert cfg.saldo_inicial == 500.0 and cfg.data_saldo.isoformat() == "2026-10-01"


def test_json_lista_simples():
    itens, cfg = lidos(json.dumps([{"tipo": "saida", "nome": "X", "valor": 1, "data": "2026-10-01"}]).encode())
    assert len(itens) == 1 and cfg is None


CSV_PONTO = "\n".join([
    "tipo,nome,valor,data,recorrencia,parcela_atual,parcelas_total",
    "entrada,Salário,2500.00,2026-10-05,mensal,,",
    "cartao,TV,400.00,2026-10-12,,1,6",
    "saida,Aluguel,600,2026-10-01,mensal,,",
    "",
]).encode()
ESPERADO = [("entrada", "Salário", 2500.0, "2026-10-05"), ("cartao", "TV", 400.0, "2026-10-12"), ("saida", "Aluguel", 600.0, "2026-10-01")]


@pytest.mark.parametrize("dados", [CSV_PT, CSV_PONTO], ids=["ponto-e-virgula+decimal-virgula+DD/MM/AAAA", "virgula+decimal-ponto+ISO"])
def test_csv_com_formatos_brasileiros(dados):
    itens, _ = lidos(dados)
    assert [(i.tipo, i.nome, i.valor, i.data.isoformat()) for i in itens] == ESPERADO


def test_csv_pt_br_completo():
    itens, _ = lidos(CSV_PT)
    assert [i.valor for i in itens] == [2500.0, 400.0, 600.0] and itens[1].parcela_atual == 1 and itens[1].recorrencia == "mensal"


@pytest.mark.parametrize("dados,trecho", [
    (b'[{"tipo": "saida", "nome": "X", "valor": -1, "data": "2026-10-01"}]', "linha 1: valor"),
    (b'[{"tipo": "saida", "nome": "X", "valor": 1, "data": "2026-13-01"}]', "linha 1: data"),
    (b'[1]', "cada lan"),
    (b"[", "JSON inv"),
    (b'{"x": 1}', "lista"),
    (b"[]", "Nenhum"),
    (b"tipo;nome;valor;data\nsaida;X;abc;2026-10-01\n", "linha 2: valor"),
    (b"tipo;nome;valor;data\nsaida;X;1;31/02/2026\n", "linha 2"),
    (b"tipo;nome;valor;data\nsaida;X;1;2026-10-01\nbanana;Y;1;2026-10-01\n", "linha 3: tipo"),
    (b'{"config": {"saldo_inicial": "x", "data_saldo": "2026-10-01"}, "lancamentos": [{"tipo":"saida","nome":"X","valor":1,"data":"2026-10-01"}]}', "config"),
    ("tipo;nome;valor;data\nsaida;é;1;2026-10-01\n".encode("latin-1"), "UTF-8"),
])
def test_erros_apontam_a_linha(dados, trecho):
    with pytest.raises(importar.ErroImportacao, match=trecho):
        importar.ler(dados)


def test_todas_as_linhas_ruins_sao_listadas():
    with pytest.raises(importar.ErroImportacao) as e:
        importar.ler(b"tipo;nome;valor;data\nx;A;1;2026-10-01\nsaida;B;-1;2026-10-01\nsaida;C;1;2026-10-01\n")
    assert len(e.value.erros) == 2 and e.value.erros[0].startswith("linha 2") and e.value.erros[1].startswith("linha 3")


# ------------------------------------------------------------- idempotência

def test_importar_duas_vezes_nao_duplica():
    repo = RepoMemoria()
    itens, cfg = lidos(JSON_OBJ)
    r1 = importar.importar(repo, itens, cfg)
    assert (r1["criados"], r1["atualizados"], r1["iguais"]) == (2, 0, 0) and r1["config"] is True
    ids = set(repo.colecoes["lancamentos"])
    r2 = importar.importar(repo, *lidos(JSON_OBJ))
    assert (r2["criados"], r2["atualizados"], r2["iguais"]) == (0, 0, 2) and set(repo.colecoes["lancamentos"]) == ids
    assert repo.colecoes["config"]["caixa"]["saldo_inicial"] == 500.0


def test_mesmo_conteudo_em_csv_e_json_gera_os_mesmos_ids():
    a, b = RepoMemoria(), RepoMemoria()
    importar.importar(a, *lidos(CSV_PT)[:1])
    importar.importar(b, [caixa.LancamentoIn(**{**l, "origem": "importado"}) for l in [
        {"tipo": "entrada", "nome": "Salário", "valor": 2500, "data": "2026-10-05", "recorrencia": "mensal"},
        {"tipo": "cartao", "nome": "TV", "valor": 400, "data": "2026-10-12", "parcela_atual": 1, "parcelas_total": 6},
        {"tipo": "saida", "nome": "Aluguel", "valor": 600, "data": "2026-10-01", "recorrencia": "mensal"}]])
    assert set(a.colecoes["lancamentos"]) == set(b.colecoes["lancamentos"])


def test_repetidos_dentro_do_arquivo_colapsam():
    l = {"tipo": "saida", "nome": "X", "valor": 1, "data": "2026-10-01"}
    r = importar.importar(RepoMemoria(), importar.ler(json.dumps([l, l, l]).encode())[0])
    assert r["lidos"] == 3 and r["repetidos_no_arquivo"] == 2 and r["criados"] == 1


def test_simular_nao_grava():
    repo = RepoMemoria()
    r = importar.importar(repo, *lidos(JSON_OBJ), simular=True)
    assert r["simulado"] and r["criados"] == 2 and "lancamentos" not in repo.colecoes and "config" not in repo.colecoes


def test_alteracao_na_origem_cria_novo_id_e_sincronizar_remove_o_velho():
    repo = RepoMemoria()
    importar.importar(repo, *lidos(JSON_OBJ))
    repo.doc_set("lancamentos", "manual1", {**caixa.para_doc(caixa.LancamentoIn(tipo="saida", nome="Dentista", valor=250, data="2026-10-20")), "origem": "manual"})
    editado = json.dumps([{"tipo": "entrada", "nome": "Salário", "valor": 2600, "data": "2026-10-05", "recorrencia": "mensal"}]).encode()
    sem = importar.importar(repo, *lidos(editado))
    assert sem["criados"] == 1 and sem["removidos"] == 0 and len(repo.colecoes["lancamentos"]) == 4   # sem sincronizar: nada some
    com = importar.importar(repo, *lidos(editado), sincronizar=True)
    assert com["iguais"] == 1 and com["removidos"] == 2                                              # salário antigo (2500) e TV
    nomes = sorted(d["nome"] for d in repo.colecoes["lancamentos"].values())
    assert nomes == ["Dentista", "Salário"]                                                           # manual nunca é removido
    assert [d["valor"] for d in repo.colecoes["lancamentos"].values() if d["nome"] == "Salário"] == [2600]


def test_sincronizar_simulado_so_conta():
    repo = RepoMemoria()
    importar.importar(repo, *lidos(JSON_OBJ))
    r = importar.importar(repo, *lidos(b'[{"tipo":"saida","nome":"X","valor":1,"data":"2026-10-01"}]'), sincronizar=True, simular=True)
    assert r["removidos"] == 2 and len(repo.colecoes["lancamentos"]) == 2


def test_erro_em_uma_linha_nao_grava_nada():
    repo = RepoMemoria()
    with pytest.raises(importar.ErroImportacao):
        importar.ler(b'[{"tipo":"saida","nome":"OK","valor":1,"data":"2026-10-01"},{"tipo":"saida","nome":"RUIM","valor":0,"data":"2026-10-01"}]')
    assert "lancamentos" not in repo.colecoes


# ------------------------------------------------------------- dados reais

def test_exemplo_importado_reproduz_a_projecao():
    repo = RepoMemoria()
    dados = json.dumps({"config": {"saldo_inicial": REL["saldo_inicial"], "data_saldo": REL["inicio"]}, "lancamentos": REL["lancamentos"]}).encode()
    itens, cfg = lidos(dados)
    importar.importar(repo, itens, cfg)
    antes = set(repo.colecoes["lancamentos"])
    assert importar.importar(repo, *lidos(dados))["iguais"] == len(itens) and set(repo.colecoes["lancamentos"]) == antes
    ls = sorted((caixa.de_doc(d) for d in repo.colecoes["lancamentos"].values()), key=lambda l: l["data"])
    cfg_doc = repo.colecoes["config"]["caixa"]
    p = caixa.projetar(ls, caixa.date.fromisoformat(cfg_doc["data_saldo"]), 12, cfg_doc["saldo_inicial"])
    assert [m["saldo_final"] for m in p["meses"]] == pytest.approx(REL["fin"], abs=0.005) and p["performance_total"] == 8245.0


def test_seed_inicial_e_valido_e_idempotente():
    arq = FIXTURES.parent.parent / "dados" / "lancamentos_iniciais.json"
    if not arq.exists():
        pytest.skip("dados/lancamentos_iniciais.json não está nesta máquina (fica fora do git)")
    itens, cfg = lidos(arq.read_bytes())
    assert len(itens) >= 1 and cfg.saldo_inicial is not None
    repo = RepoMemoria()
    assert importar.importar(repo, itens, cfg)["criados"] == len(itens)
    assert importar.importar(repo, itens, cfg)["iguais"] == len(itens)


# ------------------------------------------------------------- rota

def test_rota_exige_pin(anon):
    assert anon.post("/api/caixa/importar", files={"arquivo": ("a.json", b"[]")}).status_code == 401


def test_rota_importa_simula_e_sincroniza(client, repo):
    f = {"arquivo": ("lancamentos.json", JSON_OBJ, "application/json")}
    s = client.post("/api/caixa/importar?simular=true", files=f).json()
    assert s["simulado"] and s["criados"] == 2 and "lancamentos" not in repo.colecoes
    r = client.post("/api/caixa/importar", files=f).json()
    assert r["criados"] == 2 and len(repo.colecoes["lancamentos"]) == 2
    assert client.post("/api/caixa/importar", files=f).json()["iguais"] == 2
    assert len(client.get("/api/caixa/lancamentos").json()["lancamentos"]) == 2
    csv_ = {"arquivo": ("lancamentos.csv", CSV_PT, "text/csv")}
    assert client.post("/api/caixa/importar?sincronizar=true", files=csv_).json()["removidos"] == 0   # salário e TV também estão no CSV


def test_rota_erros(client, repo):
    r = client.post("/api/caixa/importar", files={"arquivo": ("a.csv", b"tipo;nome;valor;data\nx;A;1;2026-10-01\n")})
    assert r.status_code == 422 and "Nada foi importado" in r.json()["detail"] and "linha 2" in r.json()["detail"]
    assert "lancamentos" not in repo.colecoes
    grande = b"[" + b" " * importar.MAX_BYTES + b"]"
    assert client.post("/api/caixa/importar", files={"arquivo": ("a.json", grande)}).status_code == 413

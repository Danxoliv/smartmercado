"""Fase 2: CNPJ -> tipo de loja (BrasilAPI), categorias novas e camadas de categorização."""

import json

import httpx
import pytest

from smartmercado import classificar, cnpj
from smartmercado.categorias import CATEGORIAS, categorizar

from .conftest import FIXTURES, RepoMemoria, gerar_chave

IA_REAL = classificar.classificar_ia  # capturadas antes do stub autouse (sem_rede_externa)
CONSULTA_REAL = cnpj.consultar_brasilapi


# ------------------------------------------------------------- categorias novas (camada 2)

@pytest.mark.parametrize("nome,cat", [
    ("DIPIRONA 500MG 10CP", "Saúde/Farmácia"), ("VITAMINA C 1G EFERVESCENTE", "Saúde/Farmácia"),
    ("GASOLINA COMUM", "Combustível"), ("ETANOL HIDRATADO", "Combustível"), ("DIESEL S10", "Combustível"),
    ("OLEO MOTOR 5W30 1L", "Carro"), ("OLEO DE SOJA LIZA 900ML", "Mercearia"), ("PNEU ARO 14", "Carro"),
    ("PANELA ANTIADERENTE 24CM", "Casa"), ("LENCOL SOLTEIRO", "Casa"),
    ("CAMISETA ALGODAO M", "Roupas"), ("CHINELO HAVAIANAS", "Roupas"),
    ("CADERNO 10 MATERIAS", "Educação"), ("INGRESSO CINEMA", "Lazer"),
    ("ASSINATURA NETFLIX", "Assinaturas"), ("ALUGUEL OUTUBRO", "Moradia"), ("PASSAGEM ONIBUS", "Transporte"),
    ("AGUA SANITARIA QBOA 1L", "Limpeza"), ("PAPEL HIG NEVE", "Higiene"),   # as de mercado continuam
])
def test_categorias_novas(nome, cat):
    assert categorizar(nome) == cat


def test_categorias_validas_para_a_api():
    for c in ("Saúde/Farmácia", "Casa", "Combustível", "Transporte", "Educação", "Lazer", "Roupas", "Assinaturas", "Moradia", "Carro", "Mercearia", "Outros"):
        assert c in CATEGORIAS


# ------------------------------------------------------------- CNAE -> tipo

@pytest.mark.parametrize("cnae,tipo", [
    (4711301, "mercado"), (4712100, "mercado"), (4721102, "padaria"), (4721104, "mercado"), (4771701, "farmacia"),
    (4731800, "posto"), ("5611-2/01", "restaurante"), (4753900, "casa_utilidades"), (4781400, "vestuario"),
    (8512100, "educacao"), (4923002, "transporte"), (9001901, "lazer"), (9602501, "servicos"),
    (111301, None), (6422100, None), (None, None), ("", None),
])
def test_tipo_por_cnae(cnae, tipo):
    assert cnpj.tipo_por_cnae(cnae) == tipo


# ------------------------------------------------------------- BrasilAPI (rede simulada)

def _cliente(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_consulta_brasilapi_ok_e_host_fixo(monkeypatch):
    visto = []

    def handler(req):
        visto.append(req)
        return httpx.Response(200, json={"cnae_fiscal": 4731800, "cnae_fiscal_descricao": "Comércio varejista de combustíveis",
                                          "razao_social": "POSTO X", "nome_fantasia": "X", "outro": "ignorado"})

    d = CONSULTA_REAL("11.222.333/0001-81", _cliente(handler))
    assert d == {"cnae_fiscal": 4731800, "cnae_fiscal_descricao": "Comércio varejista de combustíveis", "razao_social": "POSTO X", "nome_fantasia": "X"}
    assert str(visto[0].url) == "https://brasilapi.com.br/api/cnpj/v1/11222333000181"


@pytest.mark.parametrize("resp", [
    httpx.Response(404), httpx.Response(500), httpx.Response(302, headers={"location": "https://evil.com/"}),
    httpx.Response(200, text="não é json"), httpx.Response(200, content=b"x" * (cnpj.MAX_BYTES + 10)),
])
def test_consulta_falha_silenciosa(resp):
    assert CONSULTA_REAL("11222333000181", _cliente(lambda r: resp)) is None


def test_consulta_timeout_e_cnpj_invalido():
    def estoura(req):
        raise httpx.ConnectTimeout("lento")

    assert CONSULTA_REAL("11222333000181", _cliente(estoura)) is None
    assert CONSULTA_REAL("123", _cliente(estoura)) is None


def test_cache_no_firestore(monkeypatch):
    repo, chamadas = RepoMemoria(), []

    def consulta(c, client=None):
        chamadas.append(c)
        return {"cnae_fiscal": 4771701, "cnae_fiscal_descricao": "Drogarias", "razao_social": "D", "nome_fantasia": None}

    monkeypatch.setattr(cnpj, "consultar_brasilapi", consulta)
    assert cnpj.tipo_do_cnpj("98765432000110", repo) == "farmacia"
    assert cnpj.tipo_do_cnpj("98765432000110", repo) == "farmacia"
    assert chamadas == ["98765432000110"]                                  # segunda vez vem do cache
    assert repo.colecoes["cnpjs"]["98765432000110"]["tipo"] == "farmacia"


def test_cache_e_rede_nunca_quebram_o_fluxo(monkeypatch):
    class RepoQuebrado(RepoMemoria):
        def doc_get(self, *a):
            raise RuntimeError("firestore fora")

        def doc_set(self, *a):
            raise RuntimeError("firestore fora")

    monkeypatch.setattr(cnpj, "consultar_brasilapi", lambda c, client=None: {"cnae_fiscal": 4731800})
    assert cnpj.tipo_do_cnpj("11222333000181", RepoQuebrado()) == "posto"
    monkeypatch.setattr(cnpj, "consultar_brasilapi", lambda c, client=None: None)
    assert cnpj.tipo_do_cnpj("11222333000181", RepoMemoria()) is None      # sem rede: None, sem erro


# ------------------------------------------------------------- camadas

def _item(nome, ean=None, cat=None):
    return {"nome": nome, "ean": ean, "categoria": cat or categorizar(nome)}


def test_camada_3_padrao_do_tipo_de_loja():
    itens = [_item("GASOLINA COMUM"), _item("ADITIVO RADIADOR"), _item("XPTO MISTERIOSO"), _item("AGUA MINERAL 500ML")]
    classificar.classificar_itens(itens, "posto", {})
    assert [i["categoria"] for i in itens] == ["Combustível", "Carro", "Combustível", "Bebidas"]   # palavra-chave vence o padrão
    soltos = [_item("XPTO MISTERIOSO")]
    classificar.classificar_itens(soltos, "farmacia", {})
    assert soltos[0]["categoria"] == "Saúde/Farmácia"
    mercado = [_item("XPTO MISTERIOSO")]
    classificar.classificar_itens(mercado, "mercado", {})
    assert mercado[0]["categoria"] == "Outros"                                                     # mercado não tem padrão


def test_camada_1_correcao_vence_tudo_e_e_reaplicada():
    repo = RepoMemoria()
    item = _item("DIPIRONA 500MG", ean="7893500018322")
    assert item["categoria"] == "Saúde/Farmácia"
    classificar.gravar_correcoes([{**item, "categoria": "Casa", "categoria_manual": True}], repo)
    assert repo.colecoes["correcoes"]["ean:7893500018322"]["categoria"] == "Casa"
    outro = _item("DIPIRONA GENERICO 1G", ean="7893500018322")                                      # nome diferente, mesmo EAN
    classificar.classificar_itens([outro], "farmacia", classificar.carregar_correcoes(repo))
    assert outro["categoria"] == "Casa"
    sem_ean = {**_item("PRODUTO SEM CODIGO"), "categoria_manual": True, "categoria": "Lazer"}
    classificar.gravar_correcoes([sem_ean], repo)
    assert "nome:PRODUTO SEM CODIGO" in repo.colecoes["correcoes"]
    classificar.gravar_correcoes([{**_item("NAO MEXEU"), "categoria_manual": False}], repo)
    assert "nome:NAO MEXEU" not in repo.colecoes["correcoes"]


def test_correcao_com_categoria_invalida_e_ignorada():
    repo = RepoMemoria()
    repo.doc_set("correcoes", "nome:X", {"categoria": "Inventada"})
    assert classificar.carregar_correcoes(repo) == {}


def test_camada_4_gemini_em_lote_so_para_outros():
    chamadas = []

    def ia(nomes, tipo):
        chamadas.append((nomes, tipo))
        return ["Casa", "Inventada"][:len(nomes)]

    itens = [_item("ARROZ TIO JOAO"), _item("COISA UM"), _item("COISA DOIS")]
    classificar.classificar_itens(itens, "mercado", {}, ia)
    assert len(chamadas) == 1 and chamadas[0] == (["COISA UM", "COISA DOIS"], "mercado")          # 1 chamada, só os "Outros"
    assert [i["categoria"] for i in itens] == ["Mercearia", "Casa", "Outros"]                    # categoria fora da lista é ignorada
    cheio = [_item("ARROZ")]
    classificar.classificar_itens(cheio, "mercado", {}, lambda n, t: pytest.fail("não deveria chamar a IA"))


def test_gemini_ia_usa_schema_fechado(monkeypatch):
    from google import genai

    visto = {}

    class Resp:
        text = json.dumps(["Casa", "Lazer"])

    class Modelos:
        def generate_content(self, model, contents, config):
            visto.update(model=model, prompt=contents, config=config)
            return Resp()

    class Cliente:
        def __init__(self, api_key, http_options=None):
            visto["http"] = http_options
            self.models = Modelos()

    monkeypatch.setenv("GEMINI_API_KEY", "teste")
    monkeypatch.setattr(genai, "Client", Cliente)
    assert IA_REAL(["PANELA", "BOLA"], "casa_utilidades") == ["Casa", "Lazer"]
    assert visto["config"].response_mime_type == "application/json"
    assert visto["config"].response_schema["items"]["enum"] == CATEGORIAS
    assert "2 categorias" in visto["prompt"]
    assert visto["http"].timeout == 8000                                   # prazo curto na classificação


def test_ia_sem_chave_ou_com_resposta_ruim_devolve_vazio(monkeypatch):
    from google import genai

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert IA_REAL(["X"], None) == [] and IA_REAL([], None) == []

    class Resp:
        text = '["Casa"]'  # tamanho errado: pediu 2

    class Cliente:
        def __init__(self, api_key, http_options=None):
            self.models = type("M", (), {"generate_content": lambda s, **k: Resp()})()

    monkeypatch.setenv("GEMINI_API_KEY", "teste")
    monkeypatch.setattr(genai, "Client", Cliente)
    assert IA_REAL(["A", "B"], None) == []


# ------------------------------------------------------------- integração nas rotas

XML_POSTO = (FIXTURES / "nfe_posto.xml").read_bytes()


def test_xml_de_posto_usa_cnae_e_padrao(client, monkeypatch):
    monkeypatch.setattr(cnpj, "consultar_brasilapi", lambda c, client=None: {"cnae_fiscal": 4731800})
    d = client.post("/api/nfce/xml", files={"xml": ("n.xml", XML_POSTO, "text/xml")}).json()
    assert d["tipo_estabelecimento"] == "posto"
    assert [i["categoria"] for i in d["itens"]] == ["Combustível", "Bebidas"]


def test_sem_rede_o_fluxo_continua(client):
    d = client.post("/api/nfce/xml", files={"xml": ("n.xml", XML_POSTO, "text/xml")}).json()   # BrasilAPI indisponível (stub)
    assert d["tipo_estabelecimento"] is None and d["itens"][0]["categoria"] == "Combustível"     # palavra-chave resolve


def test_trocar_categoria_na_revisao_grava_e_reaplica(client, repo):
    corpo = {"chave": gerar_chave(5), "estabelecimento": "LOJA", "data": "2026-10-12T09:15:00", "total": 10.0, "origem": "foto",
             "itens": [{"nome": "DIPIRONA 500MG", "ean": "7893500018322", "qtd": 1, "unidade": "UN", "preco_unit": 10.0,
                        "preco_total": 10.0, "categoria": "Casa", "categoria_manual": True}]}
    assert client.post("/api/compras", json=corpo).status_code == 201
    assert repo.colecoes["correcoes"]["ean:7893500018322"]["categoria"] == "Casa"
    assert "categoria_manual" not in repo.docs[gerar_chave(5)]["itens"][0]                 # formato de `notas` inalterado
    # nova nota com o mesmo EAN: a correção é reaplicada na extração
    xml = (FIXTURES / "nfe_farmacia.xml").read_bytes().replace(b"<cEAN>7891000100103</cEAN>", b"<cEAN>7893500018322</cEAN>")
    xml = xml.replace(b"<cEANTrib>7891000100103</cEANTrib>", b"<cEANTrib>7893500018322</cEANTrib>")
    d = client.post("/api/nfce/xml", files={"xml": ("n.xml", xml, "text/xml")}).json()
    assert d["itens"][0]["categoria"] == "Casa" and d["itens"][1]["categoria"] == "Higiene"   # PROTETOR SOLAR: palavra-chave vence o padrão da farmácia
    # editar a compra salva sem mexer em categorias não cria correções novas
    antes = dict(repo.colecoes["correcoes"])
    corpo["itens"][0]["categoria_manual"] = False
    assert client.put(f"/api/compras/{gerar_chave(5)}", json=corpo).status_code == 200
    assert repo.colecoes["correcoes"] == antes


def test_ia_faz_no_maximo_uma_chamada_mesmo_quando_falha(monkeypatch):
    from google import genai

    chamadas = []

    class Cliente:
        def __init__(self, api_key, http_options=None):
            self.models = type("M", (), {"generate_content": lambda s, **k: (chamadas.append(k["model"]), 1 / 0)[1]})()

    monkeypatch.setenv("GEMINI_API_KEY", "teste")
    monkeypatch.setattr(genai, "Client", Cliente)
    assert IA_REAL(["A", "B"], None) == [] and len(chamadas) == 1

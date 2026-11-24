"""Servidor local de demonstração com banco EM MEMÓRIA (nada vai para o Firestore). Só para ver/testar o frontend:

    python -m uvicorn tests.dev_memoria:app --port 8002        (PIN: 246813)

Começa com 3 notas de outubro/2026 e lançamentos fictícios do caixa (tests/fixtures/caixa_exemplo.json).
"""

import json
from datetime import datetime

from smartmercado import caixa
from smartmercado.db import get_repo
from smartmercado.main import app
from smartmercado.util import TZ

from .conftest import FIXTURES, RepoMemoria, gerar_chave

repo = RepoMemoria()


def _nota(n, estab, tipo, dia, itens):
    chave = gerar_chave(n)
    total = round(sum(i[3] for i in itens), 2)
    repo.docs[chave] = {
        "chave": chave, "estabelecimento": estab, "mercado": estab, "tipo_estabelecimento": tipo, "cnpj": None,
        "data": datetime(2026, 10, dia, 14, 30, tzinfo=TZ), "total": total, "descontos": 0.0, "origem": "xml",
        "itens": [{"nome": nome, "codigo": None, "ean": ean, "qtd": 1.0, "unidade": "UN", "preco_unit": p, "preco_total": p, "categoria": cat}
                  for nome, cat, ean, p in itens],
    }


_nota(1, "MERCADINHO EXEMPLO", "mercado", 9, [("ARROZ TIO JOAO 5KG", "Mercearia", "7893500018322", 27.99), ("REFRIG COCA COLA 2L", "Bebidas", "7894900011517", 10.99),
                                            ("DET YPE NEUTRO 500ML", "Limpeza", None, 2.79), ("MANTEIGA CAGEL 200G", "Frios e Laticínios", None, 7.99)])
_nota(2, "POSTO EXEMPLO", "posto", 12, [("GASOLINA COMUM", "Combustível", None, 177.69), ("AGUA MINERAL 500ML", "Bebidas", "7894900011517", 3.0)])
_nota(3, "DROGARIA EXEMPLO", "farmacia", 14, [("DIPIRONA 500MG 10CP", "Saúde/Farmácia", "7891000100103", 7.9), ("PROTETOR SOLAR FPS50", "Higiene", None, 39.9)])

_rel = json.loads((FIXTURES / "caixa_exemplo.json").read_text(encoding="utf-8"))
for _l in _rel["lancamentos"]:
    _m = caixa.LancamentoIn(**_l)
    repo.doc_set(caixa.COLECAO, caixa.id_lancamento(_m), caixa.para_doc(_m))
repo.doc_set(*caixa.CONFIG, {"saldo_inicial": _rel["saldo_inicial"], "data_saldo": _rel["inicio"]})

app.dependency_overrides[get_repo] = lambda: repo

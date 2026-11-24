import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.environ["APP_PIN"] = "246813"

from fastapi.testclient import TestClient  # noqa: E402

from smartmercado import auth  # noqa: E402
from smartmercado.db import NotaDuplicada, get_repo  # noqa: E402
from smartmercado.main import app  # noqa: E402
from smartmercado.util import dv_chave  # noqa: E402

FIXTURES = RAIZ / "tests" / "fixtures"
PIN = os.environ["APP_PIN"]
CHAVE_FIXTURE = "52261012345678000195650010000123451876543212"


def gerar_chave(n: int, cnpj: str = "12345678000195") -> str:
    base = f"522610{cnpj}65001{n:09d}1{n:08d}"
    return base + str(dv_chave(base))


class RepoMemoria:
    """Substitui o Firestore nos testes, com a mesma semântica (create falha se existir)."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.colecoes: dict[str, dict[str, dict]] = {}

    def criar(self, chave, doc):
        if chave in self.docs:
            raise NotaDuplicada(chave)
        self.docs[chave] = {**doc, "chave": chave}

    def listar(self, inicio: datetime | None = None, fim: datetime | None = None):
        r = [d for d in self.docs.values()
             if (inicio is None or d["data"] >= inicio) and (fim is None or d["data"] < fim)]
        return sorted(r, key=lambda d: d["data"], reverse=True)

    def atualizar(self, chave, doc):
        if chave not in self.docs:
            return False
        self.docs[chave] = {**self.docs[chave], **doc}
        return True

    def doc_get(self, colecao, id):
        if colecao == "notas":
            return self.docs.get(id)
        return self.colecoes.get(colecao, {}).get(id)

    def doc_set(self, colecao, id, doc):
        self.colecoes.setdefault(colecao, {})[id] = doc

    def doc_listar(self, colecao):
        return [{**d, "id": i} for i, d in self.colecoes.get(colecao, {}).items()]

    def doc_excluir(self, colecao, id):
        return self.colecoes.get(colecao, {}).pop(id, None) is not None

    def excluir(self, chave):
        return self.docs.pop(chave, None) is not None


@pytest.fixture
def html_sintetico() -> str:
    return (FIXTURES / "nfce_go_sintetica.html").read_text(encoding="utf-8")


@pytest.fixture
def repo():
    r = RepoMemoria()
    app.dependency_overrides[get_repo] = lambda: r
    yield r
    app.dependency_overrides.clear()


@pytest.fixture
def client(repo):
    auth._resetar()
    with TestClient(app, headers={"X-App-Pin": PIN}) as c:
        yield c
    auth._resetar()


@pytest.fixture
def anon(repo):
    auth._resetar()
    with TestClient(app) as c:
        yield c
    auth._resetar()


@pytest.fixture(autouse=True)
def sem_rede_externa(monkeypatch):
    """Nenhum teste chama BrasilAPI nem Gemini de verdade (testes específicos substituem estes stubs)."""
    from smartmercado import classificar, cnpj

    monkeypatch.setattr(cnpj, "consultar_brasilapi", lambda c, client=None: None)
    monkeypatch.setattr(classificar, "classificar_ia", lambda nomes, tipo: [])

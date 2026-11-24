"""Persistência no Firestore. A chave de acesso da NFC-e é o ID do documento."""

import json
import os
import threading
from datetime import datetime
from typing import Protocol

COLECAO = "notas"


class NotaDuplicada(Exception):
    pass


class ErroConfiguracao(RuntimeError):
    pass


class Repositorio(Protocol):
    def criar(self, chave: str, doc: dict) -> None: ...
    def listar(self, inicio: datetime | None = None, fim: datetime | None = None) -> list[dict]: ...
    def atualizar(self, chave: str, doc: dict) -> bool: ...
    # outras coleções (cnpjs, correcoes, lancamentos): doc_listar devolve cada documento com "id"
    def doc_get(self, colecao: str, id: str) -> dict | None: ...
    def doc_set(self, colecao: str, id: str, doc: dict) -> None: ...
    def doc_listar(self, colecao: str) -> list[dict]: ...
    def doc_excluir(self, colecao: str, id: str) -> bool: ...
    def excluir(self, chave: str) -> bool: ...


def _credenciais():
    from firebase_admin import credentials

    bruto = os.environ.get("FIREBASE_CREDENTIALS")
    if bruto:
        try:
            return credentials.Certificate(json.loads(bruto))
        except (ValueError, KeyError) as e:
            raise ErroConfiguracao("FIREBASE_CREDENTIALS não é um JSON de conta de serviço válido.") from e
    arquivo = os.environ.get("FIREBASE_CREDENTIALS_FILE")
    if arquivo:  # conveniência para desenvolvimento local
        return credentials.Certificate(arquivo)
    raise ErroConfiguracao("Defina FIREBASE_CREDENTIALS com o JSON da conta de serviço.")


class RepositorioFirestore:
    def __init__(self):
        import firebase_admin
        from firebase_admin import firestore

        try:
            app = firebase_admin.get_app("smartmercado")
        except ValueError:
            app = firebase_admin.initialize_app(_credenciais(), name="smartmercado")
        self._fs = firestore
        self._db = firestore.client(app)
        self._col = self._db.collection(COLECAO)

    def criar(self, chave: str, doc: dict) -> None:
        from google.api_core.exceptions import AlreadyExists

        doc = {**doc, "criado_em": self._fs.SERVER_TIMESTAMP}
        try:
            self._col.document(chave).create(doc)  # falha se já existir: sem duplicatas
        except AlreadyExists as e:
            raise NotaDuplicada(chave) from e

    def listar(self, inicio: datetime | None = None, fim: datetime | None = None) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self._col
        if inicio:
            q = q.where(filter=FieldFilter("data", ">=", inicio))
        if fim:
            q = q.where(filter=FieldFilter("data", "<", fim))
        q = q.order_by("data", direction=self._fs.Query.DESCENDING)
        return [{**d.to_dict(), "chave": d.id} for d in q.stream()]

    def atualizar(self, chave: str, doc: dict) -> bool:
        from google.api_core.exceptions import NotFound

        try:
            self._col.document(chave).update(doc)  # mantém criado_em; a lista de itens é substituída
        except NotFound:
            return False
        return True

    def doc_get(self, colecao: str, id: str) -> dict | None:
        d = self._db.collection(colecao).document(id).get()
        return d.to_dict() if d.exists else None

    def doc_set(self, colecao: str, id: str, doc: dict) -> None:
        self._db.collection(colecao).document(id).set(doc)

    def doc_listar(self, colecao: str) -> list[dict]:
        return [{**d.to_dict(), "id": d.id} for d in self._db.collection(colecao).stream()]

    def doc_excluir(self, colecao: str, id: str) -> bool:
        ref = self._db.collection(colecao).document(id)
        if not ref.get().exists:
            return False
        ref.delete()
        return True

    def excluir(self, chave: str) -> bool:
        ref = self._col.document(chave)
        if not ref.get().exists:
            return False
        ref.delete()
        return True


_repo: Repositorio | None = None
_lock = threading.Lock()


def get_repo() -> Repositorio:
    """Dependência FastAPI. Inicializa o Firestore só no primeiro uso (cold start)."""
    global _repo
    if _repo is None:
        with _lock:
            if _repo is None:
                _repo = RepositorioFirestore()
    return _repo

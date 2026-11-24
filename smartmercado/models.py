"""Modelos Pydantic: entrada da API (validação estrita) e saída da extração."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .categorias import CATEGORIAS, TIPOS_ESTABELECIMENTO
from .util import TZ, chave_aceita, gtin_valido, limpar_espacos, so_digitos

TOLERANCIA = 0.05  # R$ de diferença aceita entre soma dos itens e total


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nome: str = Field(min_length=1, max_length=200)
    codigo: str | None = Field(default=None, max_length=60)
    ean: str | None = None
    qtd: float = Field(gt=0, le=100000)
    unidade: str = Field(default="UN", max_length=10)
    preco_unit: float = Field(ge=0, le=1_000_000)
    preco_total: float = Field(ge=0, le=1_000_000)
    categoria: str
    categoria_manual: bool = False  # trocada pelo usuário na revisão: vira correção; não é gravada na nota

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        v = limpar_espacos(v)
        if not v:
            raise ValueError("nome vazio")
        return v

    @field_validator("unidade")
    @classmethod
    def _unidade(cls, v: str) -> str:
        return limpar_espacos(v).upper() or "UN"

    @field_validator("ean")
    @classmethod
    def _ean(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if not gtin_valido(v):
            raise ValueError("EAN inválido")
        return v

    @field_validator("categoria")
    @classmethod
    def _categoria(cls, v: str) -> str:
        if v not in CATEGORIAS:
            raise ValueError(f"categoria deve ser uma de: {', '.join(CATEGORIAS)}")
        return v


class NotaIn(BaseModel):
    """Corpo de POST /api/compras e PUT /api/compras/{chave}."""

    model_config = ConfigDict(extra="forbid")

    chave: str | None = None  # vazio = cupom sem chave fiscal; o servidor gera o ID
    estabelecimento: str | None = Field(default=None, max_length=200)
    mercado: str | None = Field(default=None, max_length=200)  # legado: espelha estabelecimento
    tipo_estabelecimento: str | None = None
    cnpj: str | None = None
    data: datetime
    total: float = Field(ge=0, le=1_000_000)
    descontos: float = Field(default=0, ge=0, le=1_000_000)
    origem: Literal["qrcode", "foto", "xml"]
    itens: list[ItemIn] = Field(min_length=1, max_length=500)

    @field_validator("chave", mode="before")
    @classmethod
    def _chave(cls, v):
        d = so_digitos(str(v or ""))
        if not d or set(d) == {"0"}:
            return None
        if not chave_aceita(d):
            raise ValueError("chave de acesso inválida (44 dígitos com DV correto)")
        return d

    @field_validator("cnpj", mode="before")
    @classmethod
    def _cnpj(cls, v):
        if v in (None, ""):
            return None
        d = so_digitos(str(v))
        if len(d) != 14:
            raise ValueError("CNPJ deve ter 14 dígitos")
        return d

    @field_validator("estabelecimento", "mercado")
    @classmethod
    def _nome(cls, v: str | None) -> str | None:
        return limpar_espacos(v) or None if v else None

    @field_validator("tipo_estabelecimento")
    @classmethod
    def _tipo(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if v not in TIPOS_ESTABELECIMENTO:
            raise ValueError(f"tipo_estabelecimento deve ser um de: {', '.join(TIPOS_ESTABELECIMENTO)}")
        return v

    @model_validator(mode="after")
    def _estabelecimento(self):
        nome = self.estabelecimento or self.mercado
        if not nome:
            raise ValueError("estabelecimento vazio")
        self.estabelecimento = self.mercado = nome
        return self

    @model_validator(mode="after")
    def _data_com_fuso(self):
        if self.data.tzinfo is None:
            self.data = self.data.replace(tzinfo=TZ)
        return self


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=10, max_length=2048)


def conferir_soma(nota: dict) -> dict:
    """Acrescenta soma_itens, diferenca e soma_confere (soma − descontos ≈ total)."""
    soma = round(sum(float(i.get("preco_total") or 0) for i in nota.get("itens", [])), 2)
    descontos = float(nota.get("descontos") or 0)
    total = float(nota.get("total") or 0)
    if descontos and abs(soma - total) <= TOLERANCIA < abs(soma - descontos - total):
        nota["descontos"] = descontos = 0.0  # a linha do cupom já vem líquida do desconto (ex.: farmácia)
    dif = round(soma - descontos - total, 2)
    nota["estabelecimento"] = nota["mercado"] = nota.get("estabelecimento") or nota.get("mercado") or ""
    nota.setdefault("tipo_estabelecimento", None)
    nota["soma_itens"] = soma
    nota["diferenca"] = dif
    nota["soma_confere"] = abs(dif) <= TOLERANCIA
    return nota

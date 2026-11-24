"""Caixa mensal: lançamentos, projeção de saldo por dia e resumo mensal.

Lançamento (coleção `lancamentos`, id = documento):
  tipo        entrada | saida | diario | economia | cartao
  data        dia da ocorrência (ou da 1ª ocorrência/parcela); salvo como texto AAAA-MM-DD (dia de calendário, sem fuso)
  recorrencia unica | diaria | semanal | mensal | anual   (mensal repete no mesmo dia; dia 31 vira o último dia do mês)
  parcelas    parcela_atual/parcelas_total: o lançamento é UM plano mensal; `data` é a parcela `parcela_atual`
  origem      manual | importado | nota        chave_nota: ligação com a coleção `notas`

Regras (iguais às do relatório):
  performance = entradas - saídas - diários - economias - cartão
  custo de vida = saídas + diários + cartão
  saldo do dia = saldo anterior + entradas - (saídas + diários + economias + cartão)
Valores em centavos inteiros por dentro; sem erro de ponto flutuante.
"""

import calendar
import hashlib
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .db import Repositorio, get_repo
from .util import TZ, chave_aceita, limpar_espacos, normalizar_nome, para_sp, so_digitos

COLECAO = "lancamentos"
CONFIG = ("config", "caixa")
TIPOS = ("entrada", "saida", "diario", "economia", "cartao")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
CAMPOS_MES = {"entrada": "entradas", "saida": "saidas", "diario": "diarios", "economia": "economias", "cartao": "cartao"}


class LancamentoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: Literal["entrada", "saida", "diario", "economia", "cartao"]
    nome: str = Field(min_length=1, max_length=120)
    valor: float = Field(gt=0, le=10_000_000)
    data: date
    recorrencia: Literal["unica", "diaria", "semanal", "mensal", "anual"] = "unica"
    parcela_atual: int | None = Field(default=None, ge=1, le=600)
    parcelas_total: int | None = Field(default=None, ge=1, le=600)
    grupo_parcela: str | None = Field(default=None, max_length=80)
    tag: str | None = Field(default=None, max_length=60)
    origem: Literal["manual", "importado", "nota"] = "manual"
    chave_nota: str | None = None

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        v = limpar_espacos(v)
        if not v:
            raise ValueError("nome vazio")
        return v

    @field_validator("chave_nota", mode="before")
    @classmethod
    def _chave(cls, v):
        if v in (None, ""):
            return None
        d = so_digitos(str(v))
        if not chave_aceita(d):
            raise ValueError("chave_nota inválida")
        return d

    @model_validator(mode="after")
    def _parcelas(self):
        if self.parcelas_total is None:
            if self.parcela_atual is not None:
                raise ValueError("parcela_atual exige parcelas_total")
            return self
        if self.recorrencia == "unica":
            self.recorrencia = "mensal"
        elif self.recorrencia != "mensal":
            raise ValueError("parcelado precisa ser recorrência mensal")
        self.parcela_atual = self.parcela_atual or 1
        if self.parcela_atual > self.parcelas_total:
            raise ValueError("parcela_atual maior que parcelas_total")
        return self


class ConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    saldo_inicial: float = Field(ge=-10_000_000, le=10_000_000)
    data_saldo: date


# ---------------------------------------------------------------- conversões

def para_doc(l: LancamentoIn) -> dict:
    d = l.model_dump()
    d["data"] = l.data.isoformat()
    return d


def de_doc(d: dict) -> dict:
    return {**d, "data": date.fromisoformat(d["data"]) if isinstance(d["data"], str) else d["data"]}


def id_lancamento(l: LancamentoIn | dict) -> str:
    """ID determinístico (tipo+nome+data+valor+parcela): reimportar o mesmo lançamento não duplica."""
    g = (lambda k: getattr(l, k)) if isinstance(l, LancamentoIn) else l.get
    data = g("data").isoformat() if hasattr(g("data"), "isoformat") else str(g("data"))
    bruto = f"{g('tipo')}|{normalizar_nome(g('nome'))}|{data}|{round(g('valor') * 100)}|{g('parcela_atual') or ''}"
    return hashlib.sha1(bruto.encode()).hexdigest()[:20]


# ---------------------------------------------------------------- projeção

def _no_mes(ano: int, mes0: int, dia: int) -> date:
    """mes0 pode passar de 11 (vira ano seguinte); dia é limitado ao último dia do mês."""
    ano, m = ano + mes0 // 12, mes0 % 12 + 1
    return date(ano, m, min(dia, calendar.monthrange(ano, m)[1]))


def ocorrencias(l: dict, inicio: date, fim: date):
    """(data, nº da parcela ou None) de cada ocorrência do lançamento dentro de [inicio, fim]."""
    d0, rec = l["data"], l.get("recorrencia") or "unica"
    if rec == "unica":
        if inicio <= d0 <= fim:
            yield d0, None
    elif rec == "diaria":
        d = max(d0, inicio)
        while d <= fim:
            yield d, None
            d += timedelta(days=1)
    elif rec == "semanal":
        d = d0 if d0 >= inicio else d0 + timedelta(days=7 * -(-(inicio - d0).days // 7))
        while d <= fim:
            yield d, None
            d += timedelta(days=7)
    elif rec in ("mensal", "anual"):
        passo = 1 if rec == "mensal" else 12
        total, k0, i = l.get("parcelas_total"), l.get("parcela_atual") or 1, 0
        while True:
            d = _no_mes(d0.year, d0.month - 1 + i * passo, d0.day)
            if d > fim or (total and k0 + i > total):
                break
            if d >= inicio:
                yield d, (k0 + i if total else None)
            i += 1


def _centavos(v: float) -> int:
    return round(v * 100)


def _reais(c: int) -> float:
    return round(c / 100, 2)


def projetar(lancamentos: list[dict], inicio: date, meses: int = 12, saldo_inicial: float = 0.0) -> dict:
    """Expande recorrências e parcelas e devolve o saldo ao fim de cada dia, mais o resumo de cada mês."""
    m_fim = inicio.month - 1 + meses
    fim = date(inicio.year + m_fim // 12, m_fim % 12 + 1, 1) - timedelta(days=1)
    por_dia: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for l in lancamentos:
        for d, _ in ocorrencias(l, inicio, fim):
            por_dia[d][l["tipo"]] += _centavos(l["valor"])

    saldo = _centavos(saldo_inicial)
    dias, resumo = [], {}
    d = inicio
    while d <= fim:
        mes = d.strftime("%Y-%m")
        m = resumo.setdefault(mes, {"mes": mes, "saldo_inicial": saldo, "saldo_minimo": None, **{c: 0 for c in CAMPOS_MES.values()}})
        for tipo, c in por_dia.get(d, {}).items():
            m[CAMPOS_MES[tipo]] += c
            saldo += c if tipo == "entrada" else -c
        if m["saldo_minimo"] is None or saldo < m["saldo_minimo"]:
            m["saldo_minimo"], m["dia_minimo"] = saldo, d.isoformat()
        m["saldo_final"] = saldo
        dias.append({"data": d.isoformat(), "saldo": _reais(saldo)})
        d += timedelta(days=1)

    saida = []
    for m in resumo.values():
        perf = m["entradas"] - m["saidas"] - m["diarios"] - m["economias"] - m["cartao"]
        custo = m["saidas"] + m["diarios"] + m["cartao"]
        saida.append({**{k: (_reais(v) if isinstance(v, int) else v) for k, v in m.items()}, "performance": _reais(perf), "custo_vida": _reais(custo)})
    return {
        "inicio": inicio.isoformat(), "fim": fim.isoformat(), "saldo_inicial": _reais(_centavos(saldo_inicial)),
        "saldo_final": _reais(saldo), "performance_total": round(sum(m["performance"] for m in saida), 2),
        "meses": saida, "dias": dias,
    }


def resumo_parcelas(lancamentos: list[dict], hoje: date) -> list[dict]:
    """Planos de parcelas: quanto falta, quando termina e quanto ainda será pago."""
    longe = date(hoje.year + 60, 12, 31)
    saida = []
    for l in lancamentos:
        if not l.get("parcelas_total"):
            continue
        todas = list(ocorrencias(l, l["data"], longe))
        futuras = [(d, k) for d, k in todas if d > hoje]
        saida.append({
            "id": l.get("id"), "nome": l["nome"], "tipo": l["tipo"], "valor": l["valor"], "parcelas_total": l["parcelas_total"],
            "parcela_atual": (futuras[0][1] - 1 if futuras else l["parcelas_total"]) or None,
            "restantes": len(futuras), "proxima": futuras[0][0].isoformat() if futuras else None,
            "termina_em": todas[-1][0].isoformat() if todas else None,
            "total_restante": round(l["valor"] * len(futuras), 2),
        })
    return sorted(saida, key=lambda p: (p["termina_em"] or "", p["nome"]))


def sugerir_lancamento(nota: dict) -> dict:
    """Lançamento sugerido para uma nota salva (o usuário escolhe diário ou cartão e confirma)."""
    return {
        "tipo": "diario", "nome": (nota.get("estabelecimento") or nota.get("mercado") or "Compra")[:120],
        "valor": round(float(nota["total"]), 2), "data": para_sp(nota["data"]).date().isoformat(),
        "recorrencia": "unica", "origem": "nota", "chave_nota": nota["chave"],
    }


# ---------------------------------------------------------------- rotas (/api/caixa/*, atrás do PIN)

router = APIRouter(prefix="/caixa")


def _id(id: str) -> str:
    if not ID_RE.match(id):
        raise HTTPException(422, "ID inválido.")
    return id


def _lancamentos(repo: Repositorio) -> list[dict]:
    return sorted((de_doc(d) for d in repo.doc_listar(COLECAO)), key=lambda l: (l["data"], l["nome"]))


@router.get("/lancamentos")
def listar(repo: Repositorio = Depends(get_repo)):
    return {"lancamentos": [{**l, "data": l["data"].isoformat()} for l in _lancamentos(repo)]}


@router.post("/lancamentos", status_code=201)
def criar(l: LancamentoIn, repo: Repositorio = Depends(get_repo)):
    if l.chave_nota:  # um lançamento por nota
        id = f"nota-{l.chave_nota}"
        if repo.doc_get(COLECAO, id):
            raise HTTPException(409, "Esta nota já tem lançamento no caixa.")
    else:
        id = id_lancamento(l) if l.origem == "importado" else uuid.uuid4().hex
    repo.doc_set(COLECAO, id, para_doc(l))
    return {"id": id}


@router.put("/lancamentos/{id}")
def alterar(id: str, l: LancamentoIn, repo: Repositorio = Depends(get_repo)):
    if not repo.doc_get(COLECAO, _id(id)):
        raise HTTPException(404, "Lançamento não encontrado.")
    repo.doc_set(COLECAO, id, para_doc(l))
    return {"id": id}


@router.delete("/lancamentos/{id}")
def excluir(id: str, repo: Repositorio = Depends(get_repo)):
    if not repo.doc_excluir(COLECAO, _id(id)):
        raise HTTPException(404, "Lançamento não encontrado.")
    return {"ok": True}


@router.get("/config")
def ver_config(repo: Repositorio = Depends(get_repo)):
    return repo.doc_get(*CONFIG) or {"saldo_inicial": 0.0, "data_saldo": None}


@router.put("/config")
def salvar_config(c: ConfigIn, repo: Repositorio = Depends(get_repo)):
    repo.doc_set(*CONFIG, {"saldo_inicial": round(c.saldo_inicial, 2), "data_saldo": c.data_saldo.isoformat()})
    return {"ok": True}


@router.get("/projecao")
def projecao(inicio: date | None = None, meses: int = Query(12, ge=1, le=60), saldo_inicial: float | None = None,
             repo: Repositorio = Depends(get_repo)):
    """Padrão: saldo e data da configuração; sem configuração, começa no dia 1 do mês atual com saldo 0."""
    cfg = repo.doc_get(*CONFIG) or {}
    if inicio is None:
        inicio = date.fromisoformat(cfg["data_saldo"]) if cfg.get("data_saldo") else datetime.now(TZ).date().replace(day=1)
    saldo = saldo_inicial if saldo_inicial is not None else cfg.get("saldo_inicial", 0.0)
    return projetar(_lancamentos(repo), inicio, meses, saldo)


@router.get("/parcelas")
def parcelas(repo: Repositorio = Depends(get_repo)):
    return {"parcelas": resumo_parcelas(_lancamentos(repo), datetime.now(TZ).date())}


@router.get("/sugestao/{chave}")
def sugestao(chave: str, repo: Repositorio = Depends(get_repo)):
    if not chave_aceita(chave):
        raise HTTPException(422, "Chave inválida.")
    nota = repo.doc_get("notas", chave)
    if not nota:
        raise HTTPException(404, "Nota não encontrada.")
    return sugerir_lancamento({**nota, "chave": chave})

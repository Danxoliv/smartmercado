"""Importação repetível de lançamentos (arquivo JSON ou CSV que você exporta de outro app ou planilha, ou monta à mão).

Idempotente: o ID do documento é derivado de tipo+nome+data+valor+parcela (caixa.id_lancamento). Importar o mesmo arquivo
de novo não duplica nada: tudo vira "iguais". Se o arquivo tiver erro em qualquer linha, NADA é gravado.

JSON: lista de lançamentos, ou {"lancamentos": [...], "config": {"saldo_inicial": 500.0, "data_saldo": "2026-10-01"}}.
CSV (separador ; ou ,): tipo;nome;valor;data;recorrencia;parcela_atual;parcelas_total;grupo_parcela;tag
  valor aceita 1.234,56 ou 1234.56; data aceita AAAA-MM-DD ou DD/MM/AAAA. Chaves que começam com "_" são ignoradas.

`sincronizar=True` também remove os lançamentos de origem "importado" que não estão mais no arquivo (editou ou apagou lá).
Lançamentos manuais ou de nota nunca são removidos. `simular=True` só conta, sem gravar.

Linha de comando (usa o Firestore das suas credenciais locais):  python -m smartmercado.importar arquivo.json [--simular] [--sincronizar]
"""

import csv
import io
import json
import re
from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import ValidationError

from .caixa import COLECAO, CONFIG, ConfigIn, LancamentoIn, id_lancamento, para_doc
from .db import Repositorio, get_repo
from .util import parse_numero_br

MAX_BYTES = 1024 * 1024
MAX_LINHAS = 5000
CAMPOS_INT = ("parcela_atual", "parcelas_total")


class ErroImportacao(ValueError):
    def __init__(self, erros: list[str]):
        self.erros = erros
        super().__init__("; ".join(erros[:5]) + (f" (+{len(erros) - 5})" if len(erros) > 5 else ""))


def _data(txt: str) -> str:
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", txt)
    return date(int(m[3]), int(m[2]), int(m[1])).isoformat() if m else txt.strip()


def _linha_csv(l: dict) -> dict:
    """Normaliza uma linha do CSV (texto) para os campos do lançamento."""
    r = {k.strip().lower(): (v or "").strip() for k, v in l.items() if k}
    out = {k: v for k, v in r.items() if v}
    if "valor" in out:
        v = parse_numero_br(out["valor"])
        if v is None:
            raise ValueError("valor: não é um número")
        out["valor"] = v
    if "data" in out:
        out["data"] = _data(out["data"])
    for k in CAMPOS_INT:
        if k in out:
            out[k] = int(out[k])
    return out


def _erro(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
    return str(e)


def ler(dados: bytes) -> tuple[list[LancamentoIn], dict | None]:
    """Arquivo -> lançamentos validados (+ config opcional). Lança ErroImportacao com todas as linhas ruins."""
    if len(dados) > MAX_BYTES:
        raise ErroImportacao(["Arquivo grande demais (máx. 1 MB)."])
    try:
        texto = dados.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ErroImportacao(["O arquivo precisa estar em UTF-8."])
    config = None
    eh_json = texto.lstrip().startswith(("{", "["))
    if eh_json:
        try:
            bruto = json.loads(texto)
        except json.JSONDecodeError as e:
            raise ErroImportacao([f"JSON inválido: {e.msg} (linha {e.lineno})"])
        if isinstance(bruto, dict):
            config = bruto.get("config")
            bruto = bruto.get("lancamentos")
        if not isinstance(bruto, list):
            raise ErroImportacao(['JSON deve ser uma lista ou {"lancamentos": [...]}.'])
        linhas = [(i, item) for i, item in enumerate(bruto, 1)]
    else:
        sep = ";" if texto.split("\n", 1)[0].count(";") >= texto.split("\n", 1)[0].count(",") else ","
        leitor = csv.DictReader(io.StringIO(texto), delimiter=sep)
        linhas = []
        for i, l in enumerate(leitor, 2):  # linha 1 é o cabeçalho
            if any((v or "").strip() for v in l.values() if isinstance(v, str)):
                linhas.append((i, l))
    if len(linhas) > MAX_LINHAS:
        raise ErroImportacao([f"Mais de {MAX_LINHAS} lançamentos."])

    itens, erros = [], []
    for i, l in linhas:
        try:
            if not isinstance(l, dict):
                raise ValueError("cada lançamento deve ser um objeto")
            l = {k: v for k, v in l.items() if not str(k).startswith("_")} if eh_json else _linha_csv(l)
            itens.append(LancamentoIn(**{**l, "origem": "importado"}))
        except (ValidationError, ValueError, TypeError) as e:
            erros.append(f"linha {i}: {_erro(e)}")
    if config is not None:
        try:
            config = ConfigIn(**config)
        except (ValidationError, TypeError) as e:
            erros.append(f"config: {_erro(e)}")
    if erros:
        raise ErroImportacao(erros)
    if not itens:
        raise ErroImportacao(["Nenhum lançamento no arquivo."])
    return itens, config


def importar(repo, itens: list[LancamentoIn], config: ConfigIn | None = None, sincronizar: bool = False, simular: bool = False) -> dict:
    novos = {id_lancamento(l): para_doc(l) for l in itens}  # repetidos dentro do arquivo colapsam
    existentes = {d["id"]: {k: v for k, v in d.items() if k != "id"} for d in repo.doc_listar(COLECAO)}
    criados = atualizados = iguais = 0
    for id, doc in novos.items():
        if id not in existentes:
            criados += 1
        elif existentes[id] == doc:
            iguais += 1
            continue
        else:
            atualizados += 1
        if not simular:
            repo.doc_set(COLECAO, id, doc)
    removidos = [id for id, d in existentes.items() if sincronizar and d.get("origem") == "importado" and id not in novos]
    if not simular:
        for id in removidos:
            repo.doc_excluir(COLECAO, id)
        if config:
            repo.doc_set(*CONFIG, {"saldo_inicial": round(config.saldo_inicial, 2), "data_saldo": config.data_saldo.isoformat()})
    return {"simulado": simular, "lidos": len(itens), "repetidos_no_arquivo": len(itens) - len(novos), "criados": criados,
            "atualizados": atualizados, "iguais": iguais, "removidos": len(removidos), "config": bool(config)}


router = APIRouter(prefix="/caixa")


@router.post("/importar")
def rota_importar(arquivo: UploadFile = File(...), simular: bool = False, sincronizar: bool = False,
                  repo: Repositorio = Depends(get_repo)):
    dados = arquivo.file.read(MAX_BYTES + 1)
    try:
        itens, config = ler(dados)
    except ErroImportacao as e:
        raise HTTPException(413 if "grande demais" in str(e) else 422, f"Nada foi importado. {e}")
    return importar(repo, itens, config, sincronizar, simular)


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    ap = argparse.ArgumentParser(description="Importa lançamentos (JSON/CSV) para o Firestore, de forma idempotente.")
    ap.add_argument("arquivo")
    ap.add_argument("--simular", action="store_true", help="só mostra o que aconteceria")
    ap.add_argument("--sincronizar", action="store_true", help="remove lançamentos 'importado' que sumiram do arquivo")
    a = ap.parse_args()
    load_dotenv()
    try:
        itens, config = ler(open(a.arquivo, "rb").read())
    except ErroImportacao as e:
        raise SystemExit(f"Nada foi importado: {e}")
    print(json.dumps(importar(get_repo(), itens, config, a.sincronizar, a.simular), ensure_ascii=False, indent=2))

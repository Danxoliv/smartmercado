"""Categorização em camadas, aplicada à nota extraída (antes da revisão do usuário):

  1. correção anterior do usuário (coleção `correcoes`; chave = EAN ou nome normalizado)
  2. palavras-chave (categorias.categorizar, já aplicada pelos leitores)
  3. categoria padrão do tipo de estabelecimento (posto -> Combustível, farmácia -> Saúde/Farmácia...)
  4. Gemini em lote, só para o que sobrou em "Outros": lista fechada de categorias, no máximo 1 chamada por nota
"""

import json
import logging
import os
from datetime import datetime

from . import cnpj as cnpj_svc
from .categorias import CATEGORIAS, DEFAULT_POR_TIPO, OUTROS
from .gemini import MODELO_PADRAO
from .util import TZ, normalizar_nome

COLECAO = "correcoes"
MAX_IA = 80  # itens por chamada
TIMEOUT_IA_MS = 8000
log = logging.getLogger("smartmercado")


def chave_correcao(item: dict) -> str:
    return f"ean:{item['ean']}" if item.get("ean") else "nome:" + normalizar_nome(item.get("nome", ""))[:200]


def carregar_correcoes(repo) -> dict[str, str]:
    try:
        return {d["id"]: d["categoria"] for d in repo.doc_listar(COLECAO) if d.get("categoria") in CATEGORIAS}
    except Exception:
        log.warning("Não consegui ler as correções de categoria.")
        return {}


def classificar_ia(nomes: list[str], tipo: str | None) -> list[str]:
    """Uma chamada ao Gemini (texto) devolvendo uma categoria por item. [] se falhar: nunca bloqueia."""
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave or not nomes:
        return []
    try:
        from google import genai
        from google.genai import types

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={"type": "ARRAY", "items": {"type": "STRING", "enum": CATEGORIAS}},
            temperature=0,
        )
        # Os nomes vêm da nota (dado externo): entram só como texto a classificar, e a saída é limitada ao enum.
        prompt = (
            f"Classifique cada item de uma nota fiscal brasileira (estabelecimento: {tipo or 'desconhecido'}) em uma das "
            f"categorias permitidas. Responda um array JSON com exatamente {len(nomes)} categorias, na mesma ordem.\n"
            + "\n".join(f"{n}. {nome[:80]}" for n, nome in enumerate(nomes, 1))
        )
        # Uma única chamada, com prazo curto: a rota da foto já gasta até PRAZO_SEG e a função do Vercel tem 60 s.
        cliente = genai.Client(api_key=chave, http_options=types.HttpOptions(timeout=TIMEOUT_IA_MS))
        resp = cliente.models.generate_content(model=os.environ.get("GEMINI_MODEL", MODELO_PADRAO), contents=prompt, config=config)
        dados = json.loads(resp.text)
        if isinstance(dados, list) and len(dados) == len(nomes):
            return [str(c) for c in dados]
    except Exception:
        pass
    return []


def classificar_itens(itens: list[dict], tipo: str | None, correcoes: dict[str, str], ia=None) -> None:
    padrao = DEFAULT_POR_TIPO.get(tipo)
    pendentes = []
    for i in itens:
        if (cat := correcoes.get(chave_correcao(i))):
            i["categoria"] = cat
        elif i.get("categoria") not in (None, OUTROS):
            continue  # camada 2 já resolveu
        elif padrao:
            i["categoria"] = padrao
        else:
            pendentes.append(i)
    if pendentes and ia:
        for i, cat in zip(pendentes[:MAX_IA], ia([p["nome"] for p in pendentes[:MAX_IA]], tipo)):
            if cat in CATEGORIAS:
                i["categoria"] = cat


def enriquecer_nota(nota: dict, repo) -> dict:
    """Define o tipo (CNAE do CNPJ, senão o que o leitor sugeriu) e aplica as camadas aos itens."""
    tipo = cnpj_svc.tipo_do_cnpj(nota.get("cnpj"), repo) or nota.get("tipo_estabelecimento")
    nota["tipo_estabelecimento"] = tipo
    classificar_itens(nota["itens"], tipo, carregar_correcoes(repo), classificar_ia)
    return nota


def gravar_correcoes(itens: list[dict], repo) -> None:
    """Chamado ao salvar: grava a categoria dos itens que o usuário trocou na revisão. Não bloqueia o salvamento."""
    agora = datetime.now(TZ)
    for i in itens:
        if i.get("categoria_manual"):
            try:
                repo.doc_set(COLECAO, chave_correcao(i), {"categoria": i["categoria"], "nome": i["nome"], "atualizado_em": agora})
            except Exception:
                log.warning("Não consegui gravar a correção de categoria.")

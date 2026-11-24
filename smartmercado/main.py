"""API FastAPI do SmartMercado.

Rotas com chamadas bloqueantes (SEFAZ, Gemini, Firestore) são `def` síncronas: o FastAPI as
executa num threadpool e o event loop não trava.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import caixa, classificar, gemini, importar, nfce, nfe_xml, relatorios
from .auth import exigir_pin
from .categorias import CATEGORIAS
from .db import ErroConfiguracao, NotaDuplicada, Repositorio, get_repo
from .models import LinkIn, NotaIn, conferir_soma
from .util import TZ, chave_aceita, chave_sintetica, normalizar_nome

load_dotenv()
log = logging.getLogger("smartmercado")

app = FastAPI(title="SmartMercado", docs_url=None, redoc_url=None, openapi_url=None)
api = APIRouter(prefix="/api", dependencies=[Depends(exigir_pin)])

MES_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; worker-src 'self'; "
    "manifest-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


@app.middleware("http")
async def cabecalhos_seguranca(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    else:  # mesma política do vercel.json, para o ambiente local se comportar igual
        resp.headers.setdefault("Content-Security-Policy", CSP)
    return resp


@app.exception_handler(ErroConfiguracao)
async def _erro_config(request: Request, exc: ErroConfiguracao):
    log.error("Configuração: %s", exc)
    return JSONResponse({"detail": str(exc)}, status_code=503)


def _intervalo_mes(mes: str | None) -> tuple[datetime, datetime]:
    if mes is None:
        hoje = datetime.now(TZ)
        ano, m = hoje.year, hoje.month
    else:
        if not MES_RE.match(mes):
            raise HTTPException(422, "mes deve estar no formato AAAA-MM")
        ano, m = int(mes[:4]), int(mes[5:])
    inicio = datetime(ano, m, 1, tzinfo=TZ)
    fim = datetime(ano + (m == 12), m % 12 + 1, 1, tzinfo=TZ)
    return inicio, fim


# ------------------------------------------------------------ rotas

@api.get("/ping")
def ping():
    return {"ok": True}


@api.get("/categorias")
def listar_categorias():
    return CATEGORIAS


@api.post("/nfce/link")
def ler_link(body: LinkIn, repo: Repositorio = Depends(get_repo)):
    try:
        url = nfce.validar_link(body.url)
        html = nfce.baixar_pagina(url)
        nota = nfce.parse_nfce_html(html, url)
    except nfce.LinkInvalido as e:
        raise HTTPException(400, str(e))
    except nfce.ErroSefaz as e:
        raise HTTPException(502, f"{e} Tente pela foto da nota.")
    except nfce.NotaNaoReconhecida as e:
        raise HTTPException(422, f"{e} Tente pela foto da nota.")
    nota["origem"] = "qrcode"
    return conferir_soma(classificar.enriquecer_nota(nota, repo))


@api.post("/nfce/foto")
def ler_foto(foto: UploadFile = File(...), repo: Repositorio = Depends(get_repo)):
    dados = foto.file.read(gemini.MAX_IMAGEM + 1)
    if len(dados) > gemini.MAX_IMAGEM:
        raise HTTPException(413, "Imagem grande demais (máx. 4 MB).")
    mime = gemini.tipo_imagem(dados)
    if mime not in gemini.TIPOS_IMAGEM:
        raise HTTPException(415, "Envie uma imagem JPEG, PNG ou WEBP.")
    try:
        nota = gemini.ler_foto(dados, mime)
    except gemini.ErroGemini as e:
        raise HTTPException(502, str(e))
    if not nota["itens"]:
        raise HTTPException(422, "Não consegui ler itens na foto. Tente outra mais nítida.")
    nota["origem"] = "foto"
    return conferir_soma(classificar.enriquecer_nota(nota, repo))


@api.post("/nfce/xml")
def ler_xml(xml: UploadFile = File(...), repo: Repositorio = Depends(get_repo)):
    dados = xml.file.read(nfe_xml.MAX_XML + 1)
    if len(dados) > nfe_xml.MAX_XML:
        raise HTTPException(413, "XML grande demais (máx. 2 MB).")
    try:
        nota = nfe_xml.parse_nfe_xml(dados)
    except nfe_xml.XmlInvalido as e:
        raise HTTPException(422, str(e))
    nota["origem"] = "xml"
    return conferir_soma(classificar.enriquecer_nota(nota, repo))


def _doc(nota: NotaIn, chave: str) -> dict:
    d = nota.model_dump()
    d["itens"] = [{k: v for k, v in i.items() if k != "categoria_manual"} for i in d["itens"]]
    return {**d, "chave": chave, "data": nota.data.astimezone(TZ)}


@api.get("/compras")
def listar_compras(mes: str | None = None, repo: Repositorio = Depends(get_repo)):
    """Notas completas (com itens) do mês, ou todas se `mes` for omitido. Base da tabela de consulta."""
    notas = repo.listar(*_intervalo_mes(mes)) if mes else repo.listar()
    return {"compras": [relatorios.nota_publica(n) for n in notas]}


@api.post("/compras", status_code=201)
def salvar(nota: NotaIn, repo: Repositorio = Depends(get_repo)):
    # Cupom sem chave fiscal ("não é documento fiscal"): ID derivado do conteúdo, para não duplicar o mesmo cupom no dia.
    chave = nota.chave or chave_sintetica("|".join([
        normalizar_nome(nota.estabelecimento), nota.data.astimezone(TZ).date().isoformat(), f"{nota.total:.2f}",
        *(f"{normalizar_nome(i.nome)}:{i.preco_total:.2f}" for i in nota.itens),
    ]))
    try:
        repo.criar(chave, _doc(nota, chave))
    except NotaDuplicada:
        raise HTTPException(409, "Esta nota já foi cadastrada.")
    classificar.gravar_correcoes(nota.model_dump()["itens"], repo)
    return {"id": chave}


@api.put("/compras/{chave}")
def alterar(chave: str, nota: NotaIn, repo: Repositorio = Depends(get_repo)):
    if not chave_aceita(chave) or nota.chave not in (None, chave):
        raise HTTPException(422, "Chave inválida.")
    if not repo.atualizar(chave, _doc(nota, chave)):
        raise HTTPException(404, "Nota não encontrada.")
    classificar.gravar_correcoes(nota.model_dump()["itens"], repo)
    return {"id": chave}


@api.delete("/compras/{chave}")
def excluir(chave: str, repo: Repositorio = Depends(get_repo)):
    if not chave_aceita(chave):
        raise HTTPException(422, "Chave inválida.")
    if not repo.excluir(chave):
        raise HTTPException(404, "Nota não encontrada.")
    return {"ok": True}


@api.get("/painel")
def ver_painel(mes: str | None = None, repo: Repositorio = Depends(get_repo)):
    inicio, fim = _intervalo_mes(mes)
    return {"mes": inicio.strftime("%Y-%m"), **relatorios.painel(repo.listar(inicio, fim))}


@api.get("/comparativo")
def ver_comparativo(mes: str | None = None, repo: Repositorio = Depends(get_repo)):
    """Dashboard comparativo: 6 meses até `mes` (padrão: mês atual)."""
    inicio_ref, fim = _intervalo_mes(mes)
    ref = inicio_ref.strftime("%Y-%m")
    primeiro = relatorios.meses_ate(ref)[0]
    inicio = datetime(int(primeiro[:4]), int(primeiro[5:]), 1, tzinfo=TZ)
    return {"mes": ref, **relatorios.comparativo(repo.listar(inicio, fim), ref)}


@api.get("/comparar")
def comparar(q: str = Query(min_length=2, max_length=80), repo: Repositorio = Depends(get_repo)):
    return {"produtos": relatorios.comparar(repo.listar(), q)}


@api.get("/export.csv")
def exportar(mes: str | None = None, repo: Repositorio = Depends(get_repo)):
    if mes:
        inicio, fim = _intervalo_mes(mes)
        notas, nome = repo.listar(inicio, fim), f"smartmercado-{mes}.csv"
    else:
        notas, nome = repo.listar(), "smartmercado-tudo.csv"
    return Response(
        relatorios.gerar_csv(notas).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


api.include_router(importar.router)
api.include_router(caixa.router)  # /api/caixa/*, mesma proteção por PIN
app.include_router(api)

# Em desenvolvimento local o FastAPI também serve o frontend; no Vercel quem serve é a CDN.
_PUBLIC = Path(__file__).resolve().parent.parent / "public"
if _PUBLIC.is_dir():
    app.mount("/", StaticFiles(directory=_PUBLIC, html=True), name="public")

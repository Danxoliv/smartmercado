"""Leitura da nota por foto usando o Gemini com saída JSON estruturada."""

import json
import os
import time
from datetime import datetime

from .categorias import TIPOS_ESTABELECIMENTO, categorizar
from .util import TZ, chave_valida, cnpj_da_chave, cnpj_valido, gtin_valido, limpar_espacos, so_digitos

MODELO_PADRAO = "gemini-3.8-flash"
MODELOS_RESERVA = ("gemini-3.7-flash", "gemini-3.6-flash")
PRAZO_SEG = 26            # só começa nova tentativa antes disso
TIMEOUT_CHAMADA_MS = 16000  # cada chamada ao Gemini; 26 + 16 + CNPJ (~7 s) + classificação (8 s) cabem nos 60 s da função
TEMPORARIOS = ("503", "429", "404", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "NOT_FOUND", "DEADLINE_EXCEEDED", "imeout", "timed out")
MAX_IMAGEM = 4 * 1024 * 1024
TIPOS_IMAGEM = {"image/jpeg", "image/png", "image/webp"}


class ErroGemini(RuntimeError):
    pass


SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "estabelecimento": {"type": "STRING", "description": "Nome/razão social do emitente"},
        "tipo_estabelecimento": {"type": "STRING", "enum": list(TIPOS_ESTABELECIMENTO), "nullable": True},
        "cnpj": {"type": "STRING", "nullable": True, "description": "CNPJ do emitente, só dígitos"},
        "data_emissao": {
            "type": "STRING",
            "nullable": True,
            "description": "Data e hora de emissão no formato AAAA-MM-DDTHH:MM:SS",
        },
        "chave_acesso": {
            "type": "STRING",
            "nullable": True,
            "description": "Chave de acesso com 44 dígitos, sem espaços",
        },
        "valor_a_pagar": {"type": "NUMBER", "description": "Valor a pagar (já com descontos)"},
        "descontos": {"type": "NUMBER", "nullable": True},
        "itens": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "nome": {"type": "STRING"},
                    "codigo": {"type": "STRING", "nullable": True},
                    "quantidade": {"type": "NUMBER"},
                    "unidade": {"type": "STRING", "nullable": True},
                    "preco_unitario": {"type": "NUMBER"},
                    "preco_total": {"type": "NUMBER"},
                },
                "required": ["nome", "quantidade", "preco_unitario", "preco_total"],
            },
        },
    },
    "required": ["estabelecimento", "valor_a_pagar", "itens"],
}

PROMPT = """Você recebe a foto de uma nota ou cupom fiscal brasileiro (NFC-e, NF-e, cupom não fiscal) de qualquer loja ou
serviço: supermercado, farmácia, posto de combustível, restaurante, loja de utilidades, etc.
Extraia exatamente o que está impresso, sem inventar:
- estabelecimento: nome do emitente; cnpj: só dígitos.
- tipo_estabelecimento: o valor permitido que melhor descreve o emitente ("outro" se houver dúvida).
- data_emissao: data/hora de emissão (AAAA-MM-DDTHH:MM:SS); null se o cupom não tiver data.
- chave_acesso: os 44 dígitos da chave de acesso, sem espaços. Cupons "NÃO É DOCUMENTO FISCAL" não têm chave: use null, nunca invente dígitos.
- valor_a_pagar: o "Valor a pagar" ou "TOTAL" (já com descontos); descontos: total de descontos, se houver.
- itens: um por linha do cupom (produto ou serviço), na ordem. Combustível: quantidade em litros. Números com ponto decimal (27.99, não 27,99).
  preco_total é o valor total da linha. Mantenha o nome como impresso.
Se algum campo estiver ilegível, use null (nos opcionais)."""


def _num(valor) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def normalizar_resposta(bruto: dict) -> dict:
    """Converte o JSON do Gemini no mesmo formato do parser de NFC-e."""
    itens = []
    for i in bruto.get("itens") or []:
        nome = limpar_espacos(str(i.get("nome") or ""))
        qtd = _num(i.get("quantidade")) or 1.0
        total = _num(i.get("preco_total"))
        unit = _num(i.get("preco_unitario"))
        if not nome or total is None:
            continue
        if unit is None:
            unit = round(total / qtd, 4)
        codigo = so_digitos(str(i.get("codigo") or "")) or None
        itens.append({
            "nome": nome[:200],
            "codigo": codigo,
            "ean": codigo if gtin_valido(codigo) else None,
            "qtd": qtd,
            "unidade": (limpar_espacos(str(i.get("unidade") or "UN")).upper() or "UN")[:10],
            "preco_unit": unit,
            "preco_total": round(total, 2),
            "categoria": categorizar(nome),
        })

    chave = so_digitos(str(bruto.get("chave_acesso") or ""))
    if not chave_valida(chave):
        chave = ""  # cupom não fiscal ou leitura errada: o servidor gera um ID
    cnpj = so_digitos(str(bruto.get("cnpj") or ""))
    if not cnpj_valido(cnpj) and chave_valida(chave):
        cnpj = cnpj_da_chave(chave)

    data = None
    if bruto.get("data_emissao"):
        try:
            dt = datetime.fromisoformat(str(bruto["data_emissao"]))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=TZ)
            data = dt.isoformat() if abs((dt - datetime.now(TZ)).days) <= 366 else None  # ano mal lido (ex.: 2024): a tela usa a data de hoje
        except ValueError:
            data = None

    soma = round(sum(i["preco_total"] for i in itens), 2)
    descontos = _num(bruto.get("descontos")) or 0.0
    total = _num(bruto.get("valor_a_pagar"))
    return {
        "mercado": limpar_espacos(str(bruto.get("estabelecimento") or bruto.get("mercado") or "")),
        "tipo_estabelecimento": bruto.get("tipo_estabelecimento") if bruto.get("tipo_estabelecimento") in TIPOS_ESTABELECIMENTO else None,
        "cnpj": cnpj if cnpj_valido(cnpj) else "",  # CNPJ mal lido (dígito verificador errado) é pior que vazio
        "data": data,
        "chave": chave,
        "total": round(total, 2) if total is not None else round(soma - descontos, 2),
        "descontos": round(descontos, 2),
        "itens": itens,
    }


def ler_foto(imagem: bytes, mime: str) -> dict:
    """Envia a imagem ao Gemini e devolve a nota normalizada. Chamada bloqueante."""
    from google import genai
    from google.genai import types

    chave_api = os.environ.get("GEMINI_API_KEY")
    if not chave_api:
        raise ErroGemini("GEMINI_API_KEY não configurada.")
    client = genai.Client(api_key=chave_api, http_options=types.HttpOptions(timeout=TIMEOUT_CHAMADA_MS))
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=SCHEMA,
        temperature=0,
    )
    # Principal primeiro; se estiver sobrecarregado (503) ou sem cota (429), tenta os reservas.
    modelos = list(dict.fromkeys([os.environ.get("GEMINI_MODEL", MODELO_PADRAO), *MODELOS_RESERVA]))
    limite = time.monotonic() + PRAZO_SEG  # a função do Vercel tem 60 s
    ultimo_erro = ""
    for modelo in modelos:
        for tentativa in range(2):
            if time.monotonic() > limite:
                break
            try:
                resp = client.models.generate_content(
                    model=modelo,
                    contents=[types.Part.from_bytes(data=imagem, mime_type=mime), PROMPT],
                    config=config,
                )
                return normalizar_resposta(json.loads(resp.text))
            except json.JSONDecodeError as e:
                raise ErroGemini("O Gemini não devolveu um JSON válido. Tente outra foto.") from e
            except Exception as e:  # erros de rede/cota do SDK
                ultimo_erro = str(e)
                if not any(c in ultimo_erro for c in TEMPORARIOS):
                    raise ErroGemini(f"Falha ao ler a foto com o Gemini ({e.__class__.__name__}).") from e
                if tentativa == 0 and ("503" in ultimo_erro or "UNAVAILABLE" in ultimo_erro):
                    time.sleep(2)  # sobrecarga passageira: repete o mesmo modelo uma vez
                    continue
                break  # sem cota, indisponível ou demorando demais: vale o próximo modelo
    if "429" in ultimo_erro or "RESOURCE_EXHAUSTED" in ultimo_erro:
        raise ErroGemini("A cota gratuita do Gemini acabou (limite por minuto ou por dia). Espere um pouco ou use o QR Code.")
    if any(c in ultimo_erro for c in ("504", "DEADLINE_EXCEEDED", "imeout", "timed out")):
        raise ErroGemini("O Gemini demorou demais para responder. Tente de novo em instantes ou use o QR Code.")
    raise ErroGemini("O Gemini está sobrecarregado agora. Tente de novo em instantes ou use o QR Code.")


def tipo_imagem(dados: bytes) -> str | None:
    """Detecta o tipo pela assinatura do arquivo (não confia no content-type do cliente)."""
    if dados.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if dados.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if dados[:4] == b"RIFF" and dados[8:12] == b"WEBP":
        return "image/webp"
    return None

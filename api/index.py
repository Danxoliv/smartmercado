"""Ponto de entrada da Serverless Function no Vercel (todas as rotas /api/* chegam aqui)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartmercado.main import app  # noqa: E402,F401

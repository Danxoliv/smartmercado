# Scanner de NFC-e (Goiás): pacote para integrar

Dois arquivos, sem dependência do resto do SmartMercado:

| Arquivo | O que é |
|---|---|
| `nfce_scanner.py` | Backend em um arquivo só: valida o link do QR Code, baixa a nota da SEFAZ-GO, extrai os itens, lê foto com o Gemini, categoriza, e traz um `APIRouter` do FastAPI opcional |
| `nfce-scanner.js` | Frontend em um arquivo só: câmera (QR Code), foto comprimida (< 1,5 MB) e as chamadas à API |
| `test_nfce_scanner.py` + `fixtures/` | 21 testes com uma nota real de Goiás (`pytest integracao`) |

Não inclui: banco de dados, login/PIN, telas de revisão, dashboards. Isso fica no seu app.

## 1. Backend

```bash
pip install httpx beautifulsoup4 google-genai          # núcleo
pip install fastapi python-multipart                   # só se for usar o router
```

Variáveis de ambiente: `GEMINI_API_KEY` (só para foto) e, opcional, `GEMINI_MODEL` (padrão `gemini-3.8-flash`).

**Com FastAPI**, no app principal, protegendo com a sua autenticação:

```python
from fastapi import Depends
import nfce_scanner

app.include_router(nfce_scanner.criar_router([Depends(sua_autenticacao)]), prefix="/api")
# POST /api/nfce/link   {"url": "https://nfeweb.sefaz.go.gov.br/..."}
# POST /api/nfce/foto   multipart, campo "foto"
# GET  /api/nfce/categorias
```

Sem `dependencies` a rota fica aberta e qualquer pessoa gasta a sua cota do Gemini.

**Sem FastAPI** (Flask, Django, script...): chame as funções direto.

```python
nota = nfce_scanner.ler_link(url_do_qr)            # LinkInvalido | ErroSefaz | NotaNaoReconhecida
nota = nfce_scanner.ler_foto_bytes(bytes_da_foto)  # ImagemInvalida | ErroGemini | NotaNaoReconhecida
```

São funções bloqueantes (rede). Em app assíncrono, rode-as em thread.

Teste rápido: `python nfce_scanner.py "<link do QR Code>"` imprime o JSON.

## 2. Formato da nota

```json
{
  "mercado": "SUPERMERCADO EXEMPLO LTDA",
  "cnpj": "12345678000195",
  "data": "2026-01-15T10:20:30-03:00",
  "chave": "52260112345678000195651100000400899000857068",
  "total": 210.63,
  "descontos": 0.0,
  "origem": "qrcode",
  "soma_itens": 210.63, "diferenca": 0.0, "soma_confere": true,
  "itens": [{"nome": "MANTEIGA CAGEL 200G", "codigo": "7898954365268", "ean": "7898954365268",
             "qtd": 1.0, "unidade": "UN", "preco_unit": 7.99, "preco_total": 7.99, "categoria": "Frios e Laticínios"}]
}
```

- `total` é o **valor a pagar** (já com descontos). Para fechar com a soma dos itens, use `soma_itens - descontos`.
- `data` e `chave` podem vir vazios na **foto** de cupom "não é documento fiscal". Peça a data ao usuário. Para ter um ID único sem chave, use `nfce_scanner.chave_sintetica(texto)`, que gera 44 dígitos começando com `00` (chave real de Goiás começa com `52`).
- `ean` só vem preenchido quando o código passa na validação de GTIN.
- Categorias: Hortifruti, Carnes, Frios e Laticínios, Padaria, Mercearia, Bebidas, Bebidas Alcoólicas, Higiene, Limpeza, Utilidades, Pet, Outros. Para mapear às suas, troque o dicionário `PALAVRAS` no arquivo.
- Para não duplicar nota do QR Code, use `chave` como chave única no seu banco.

## 3. Frontend

```html
<script src="html5-qrcode.min.js"></script>   <!-- npm: html5-qrcode@2.3.8 (só para a câmera) -->
<script src="nfce-scanner.js"></script>
<div id="leitor"></div>
<script>
  const nfce = NfceScanner.criar({ baseUrl: "/api/nfce", headers: () => ({ Authorization: "Bearer " + token }) });
  nfce.iniciarCamera("leitor", { aoLerNota: abrirFormulario, aoErro: (e) => alert(e.message) });
  // foto: abrirFormulario(await nfce.lerFoto(input.files[0]));
</script>
```

Se o seu site tem Content-Security-Policy, libere `media-src blob:` e `img-src blob:` (câmera e foto). A câmera só funciona em HTTPS ou `localhost`.
Ao mostrar a nota na tela, use `textContent`/`value`, nunca `innerHTML`: nomes de produto vêm de fora.

## 4. Como funciona e limites

- **QR Code de Goiás em duas etapas:** a página do QR só monta um iframe. O backend abre o link (cria a sessão/cookie) e depois chama `/nfeweb/sites/nfce/render/html/danfeNFCe?chNFe=...`, que devolve um XML com o HTML da nota. Se a SEFAZ mudar esse fluxo, o ajuste fica em `baixar_pagina` e `html_do_render`.
- **Segurança:** só `https://nfeweb.sefaz.go.gov.br`, com SSL verificado, também em redirecionamentos; resposta limitada a 3 MB; imagem a 4 MB, com o tipo conferido pelos bytes.
- **Foto (Gemini):** tenta `gemini-3.8-flash` e, se estiver sobrecarregado (503) ou sem cota (429), usa `gemini-3.7-flash` e `gemini-3.6-flash`. Dentro de um prazo de 45 s, para caber em funções serverless de 60 s. A chave gratuita tem cota diária.
- **Só Goiás:** outros estados têm portais diferentes. A foto funciona para qualquer cupom, mas o QR Code, não.
- **Valores monetários** são `float` com 2 casas. Se o seu app usa centavos inteiros, converta com `round(valor * 100)`.

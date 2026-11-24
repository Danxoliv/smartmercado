# SmartMercado

App pessoal para controlar compras de supermercado a partir da **NFC-e de Goiás**.
Escaneie o QR Code do cupom (ou tire uma foto), revise os itens e salve. O app mostra o gasto
do mês por categoria e por mercado, compara preços de um produto entre mercados e exporta CSV.

**Stack:** Python 3.12 + FastAPI · Firestore · Gemini (`gemini-3.8-flash`) · HTML/CSS/JS puro (PWA) · Vercel.

## Estrutura

```
api/index.py            entrada do app FastAPI no Vercel
smartmercado/
  main.py               rotas FastAPI
  auth.py               PIN (APP_PIN) em todas as rotas /api
  nfce.py               validação do link, download (SSL ligado) e parser da página da SEFAZ-GO
  nfe_xml.py            parser do XML de NF-e/NFC-e (layout nacional; recusa DOCTYPE/ENTITY)
  gemini.py             leitura por foto (JSON + response_schema), qualquer loja
  categorias.py         palavras-chave por categoria, tipos de estabelecimento e padrão por tipo
  cnpj.py               BrasilAPI: CNPJ -> CNAE -> tipo de loja, com cache em `cnpjs`
  classificar.py        camadas de categoria: correção > palavras > padrão do tipo > Gemini em lote
  caixa.py              lançamentos, projeção de saldo por dia, parcelas, rotas /api/caixa/*
  importar.py           importação idempotente de lançamentos (JSON/CSV) + CLI
  models.py             validação Pydantic do corpo das requisições
  db.py                 Firestore (Protocol Repositorio; ID da nota = chave de acesso)
  relatorios.py         painel, comparador e CSV
public/                 frontend (CDN do Vercel): app.js, caixa.js, graficos.js (SVG próprio), styles.css
tests/                  pytest; dev_memoria.py = servidor de demonstração com banco em memória
dados/                  dados pessoais de partida (fora do git e do deploy)
vercel.json             região gru1, maxDuration, cabeçalhos de segurança
```

## API

Todas as rotas exigem o cabeçalho `X-App-Pin` (com o PIN errado: 401; depois de 5 erros: 429 por 5 min).

| Método | Rota | O que faz |
|---|---|---|
| GET | `/api/ping` | testa o PIN |
| GET | `/api/categorias` | lista de categorias |
| POST | `/api/nfce/link` | `{"url": "https://nfeweb.sefaz.go.gov.br/..."}` → nota extraída |
| POST | `/api/nfce/foto` | multipart `foto` (JPEG/PNG/WEBP até 4 MB) → nota extraída pelo Gemini (qualquer loja) |
| POST | `/api/nfce/xml` | multipart `xml` (NF-e/NFC-e de qualquer UF, até 2 MB) → nota extraída |
| GET | `/api/compras[?mes=AAAA-MM]` | compras completas, com itens (tabela da aba **Dados**); sem `mes`, todas |
| POST | `/api/compras` | salva a nota revisada (201; 409 se já existe). `chave` vazia = cupom sem chave fiscal |
| PUT | `/api/compras/{chave}` | substitui os dados de uma compra já salva (edição manual) |
| DELETE | `/api/compras/{chave}` | exclui uma compra |
| GET | `/api/painel?mes=AAAA-MM` | total, por categoria, por mercado, lista de compras |
| GET | `/api/comparativo?mes=AAAA-MM` | dashboard comparativo: 6 meses até o mês, variação vs. mês anterior, ticket médio e categorias do mês vs. anterior |
| GET | `/api/comparar?q=texto` | produtos agrupados por EAN (ou nome normalizado), preço por mercado e histórico |
| GET | `/api/export.csv[?mes=AAAA-MM]` | CSV (`;`, decimal com vírgula, UTF-8 com BOM) com `estabelecimento`, `tipo_estabelecimento` e `categoria` |
| GET/POST | `/api/caixa/lancamentos` | lista / cria lançamento (entrada, saida, diario, economia, cartao) |
| PUT/DELETE | `/api/caixa/lancamentos/{id}` | altera / exclui |
| GET | `/api/caixa/projecao[?inicio=&meses=12&saldo_inicial=]` | saldo por dia e resumo mensal (performance, custo de vida, pior dia) |
| GET | `/api/caixa/parcelas` | planos de parcelas: quanto falta, quando termina |
| GET/PUT | `/api/caixa/config` | saldo inicial e a data dele |
| GET | `/api/caixa/sugestao/{chave}` | lançamento sugerido (diário) para uma nota salva |
| POST | `/api/caixa/importar[?simular=&sincronizar=]` | importa JSON/CSV (idempotente) |

A extração devolve também `soma_itens`, `diferenca` e `soma_confere` (soma dos itens − descontos ≈ valor a pagar, tolerância de R$ 0,05).

### Como a NFC-e de Goiás é lida

A página do QR Code (`/nfeweb/sites/nfce/danfeNFCe?p=…`) não traz os itens. Ela só monta um iframe.
O backend faz o mesmo que o navegador, mantendo o cookie de sessão e validando HTTPS e o host em cada passo:

1. `GET` no link do QR Code, que cria a sessão;
2. `GET /nfeweb/sites/nfce/render/html/danfeNFCe?chNFe=<chave>`, que devolve um XML `<Map>` com o HTML do
   DANFE escapado em `DANFE_NFCE_HTML`. Se esse endereço não existir, o backend usa o iframe `/render/danfeNFCe`;
3. o parser lê o layout padrão (`.txtTopo`, `#tabResult`, `.RCod`, `.Rqtd`, `.RUN`, `.RvlUnit`, `.valor`,
   "Valor a pagar", `.chave`).

Sem o cookie, a SEFAZ devolve uma página vazia. Os testes usam uma nota real salva em `tests/fixtures/nfce_go_real_*`.

### Firestore

Coleção `notas`, um documento por compra, com **ID = chave de acesso (44 dígitos)**. O documento é
criado com `create()`, que falha se ele já existir, então a mesma nota não entra duas vezes.

```
chave, mercado (= estabelecimento, mantido para os documentos antigos), estabelecimento, tipo_estabelecimento,
cnpj, data (timestamp), total, descontos (números), origem ("qrcode" | "foto" | "xml"),
criado_em (timestamp do servidor),
itens: [{nome, codigo, ean, qtd, unidade, preco_unit, preco_total, categoria}]
```

`estabelecimento` e `tipo_estabelecimento` são opcionais: documentos antigos só têm `mercado` e continuam sendo lidos.

| Coleção | O que guarda |
|---|---|
| `notas` | uma compra por documento (formato acima) |
| `lancamentos` | caixa: `tipo, nome, valor, data (texto AAAA-MM-DD), recorrencia, parcela_atual, parcelas_total, grupo_parcela, tag, origem (manual\|importado\|nota), chave_nota`. Nota lançada tem id `nota-<chave>` |
| `correcoes` | categoria que você escolheu na revisão (id `ean:<ean>` ou `nome:<nome normalizado>`) |
| `cnpjs` | cache da BrasilAPI por CNPJ (`cnae_fiscal`, `tipo`) |
| `config` | documento `caixa`: saldo inicial e a data dele |

Sem índice composto: as consultas só filtram por `data` nas notas; o resto é lido inteiro (uso pessoal).

> O app antigo gravava na coleção `compras`, com outro formato. Essa coleção não é lida pelo app novo.

### Cupom que "não é documento fiscal"

Alguns mercados dão um cupom sem chave de acesso (e às vezes sem data). Pela foto, o app aceita a chave vazia e
usa a data de agora, que você pode corrigir na revisão. O servidor cria um ID de 44 dígitos começando com `00`,
calculado a partir de mercado, dia, total e itens. O mesmo cupom salvo duas vezes no mesmo dia dá "já cadastrada".
Chaves reais começam com o código da UF (52 em Goiás), então nunca colidem.

### Notas de qualquer loja (GO, DF e outras)

- **Link do QR Code:** só Goiás (`nfeweb.sefaz.go.gov.br`). **DF ainda não tem leitor de link**: falta uma nota real do DF para
  descobrir o endereço e o layout (a pesquisa achou dois endereços diferentes e nenhum confirmado). Enquanto isso, o DF entra por
  **foto** ou por **XML**.
- **XML da NF-e/NFC-e:** layout nacional, qualquer UF. O arquivo é recusado se tiver `DOCTYPE`/`ENTITY` (XXE e expansão de entidades),
  se não for UTF-8 ou se passar de 2 MB.
- **Tipo de loja:** pelo CNPJ, a BrasilAPI (`GET /api/cnpj/v1/{cnpj}`, campos `cnae_fiscal` e `cnae_fiscal_descricao`) devolve o CNAE,
  que vira `mercado`, `farmacia`, `posto`, `restaurante`, `casa_utilidades`, etc. (tabela em `cnpj.py`). O resultado fica em cache
  no Firestore. Se a BrasilAPI falhar, o fluxo segue sem o tipo: nunca bloqueia salvar.

### Categorias em camadas

1. **correção sua** (coleção `correcoes`): ao trocar a categoria na revisão, ela é gravada e reaplicada nas próximas notas com o mesmo EAN ou nome;
2. **palavras-chave** (abaixo);
3. **padrão do tipo de loja** (posto → Combustível, farmácia → Saúde/Farmácia, casa/utilidades → Casa, roupas → Roupas...);
4. **Gemini em lote**, só para o que ficou em "Outros": uma chamada por nota, com a lista fechada de categorias e prazo de 8 s.

Categorias: as de mercado (Hortifruti, Carnes, Frios e Laticínios, Padaria, Mercearia, Bebidas, Bebidas Alcoólicas, Higiene, Limpeza,
Utilidades, Pet) mais Saúde/Farmácia, Casa, Combustível, Transporte, Educação, Lazer, Roupas, Assinaturas, Moradia e Carro.

### Categorização por palavras-chave

Feita localmente, sem IA. O nome é normalizado (maiúsculas, sem acento, pontuação vira espaço) e cada
palavra-chave só casa como **palavra inteira** (`\b`). Assim `DET` não casa com `DETALHE` e `REFRIG` não casa com `REFRIGERADOR`.
As **frases são testadas primeiro, da mais longa para a mais curta** (`AGUA SANITARIA` vence `AGUA`,
`CREME DE LEITE` vence `LEITE`). Se nenhuma frase casar, vale a primeira palavra-chave que aparece no
nome (`SUCO ... LARANJA` → Bebidas). As abreviações de NFC-e estão incluídas: `REFRIG`, `DESOD`, `DET`,
`PAPEL HIG`, `CERV`, `QJO`, `MUSS`, `CR DENT`, `AMAC`, `LT COND` e outras. Para ajustar, edite
`smartmercado/categorias.py`. A categoria também pode ser trocada na tela de revisão.

## Caixa mensal

Aba **Caixa**: *Saldos* (mês a mês, com o pior dia), *Projeção* (12 meses, em SVG), *Parcelas* e *Lançamentos*. Regras:

- `performance = entradas − saídas − diários − economias − cartão`; `custo de vida = saídas + diários + cartão`;
- `saldo do dia = saldo anterior + entradas − (saídas + diários + economias + cartão)`, em centavos inteiros;
- recorrência `unica | diaria | semanal | mensal | anual` (dia 31 vira o último dia do mês); parcelado é **um plano mensal**
  (`parcela_atual`/`parcelas_total`), então a parcela do carro em 44x é um só lançamento até 05/2030;
- ao salvar uma nota, o app oferece lançá-la no caixa como Diário ou Cartão (`chave_nota`, um lançamento por nota).

O saldo inicial e a data dele ficam em *Saldos → Saldo inicial* (ou `PUT /api/caixa/config`).

### Importar lançamentos (repetível)

Você pode importar de qualquer app ou planilha que exporte (ou que você monte) um **JSON ou CSV**, quantas vezes quiser. Não há login automático em nenhum site:

```bash
python -m smartmercado.importar dados/lancamentos_iniciais.json --simular        # só mostra o que aconteceria
python -m smartmercado.importar dados/lancamentos_iniciais.json                  # importa
python -m smartmercado.importar arquivo-novo.csv --sincronizar            # também remove os "importado" que sumiram do arquivo
```

Ou pela aba *Lançamentos → Importar*, ou `POST /api/caixa/importar`. O ID vem de tipo + nome + data + valor + parcela, então reimportar
o mesmo arquivo dá "iguais" e não duplica. Se uma linha tiver erro, nada é gravado. Lançamentos manuais ou de nota nunca são removidos.

- JSON: lista, ou `{"config": {"saldo_inicial": 500.0, "data_saldo": "2026-10-01"}, "lancamentos": [...]}`;
- CSV (`;` ou `,`): `tipo;nome;valor;data;recorrencia;parcela_atual;parcelas_total;grupo_parcela;tag`, valor `1.234,56` ou `1234.56`,
  data `AAAA-MM-DD` ou `DD/MM/AAAA`.

`dados/lancamentos_iniciais.json` (opcional) guarda os seus dados de partida. A pasta `dados/` **não** vai para o git nem para o Vercel.

## Rodar localmente

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements-dev.txt
copy .env.example .env          # preencha APP_PIN, GEMINI_API_KEY e as credenciais
uvicorn smartmercado.main:app --reload --port 8000
```

Abra http://localhost:8000. Em desenvolvimento, você pode usar `FIREBASE_CREDENTIALS_FILE=firebase_credentials.json`
no `.env` em vez de colar o JSON. A câmera funciona em `localhost`. Para testar no celular, use o deploy, que tem HTTPS.

### Ver o app sem tocar no Firestore

```bash
python -m uvicorn tests.dev_memoria:app --port 8002      # PIN 246813; 3 notas de exemplo + lançamentos fictícios do caixa
```

### Testes

```bash
pytest
```

Os testes não acessam a internet nem o Firestore: o banco é substituído por um repositório em memória,
e a SEFAZ e o Gemini são simulados.

## Deploy no Vercel (passo a passo)

1. **Firebase**
   1. No [console do Firebase](https://console.firebase.google.com), crie o projeto e ative o **Firestore** (modo produção, região `southamerica-east1`).
   2. Em *Configurações do projeto → Contas de serviço → Gerar nova chave privada*, baixe o JSON.
      **Não** coloque esse arquivo no git: o `.gitignore` já ignora `firebase_credentials.json`, `*credentials*.json` e `.env`.
   3. Gere o JSON em uma linha para colar no Vercel:
      ```bash
      python -c "import json;print(json.dumps(json.load(open('firebase_credentials.json'))))"
      ```
   4. As regras do Firestore podem negar todo acesso de cliente (`allow read, write: if false;`).
      O app só acessa o banco pelo backend, com a conta de serviço.

2. **Gemini**: crie a chave em https://aistudio.google.com/apikey.

3. **Repositório**: suba o código para o GitHub, num repositório **privado**:
   ```bash
   git init
   git add .
   git status                     # confira que .env e firebase_credentials.json NÃO aparecem
   git commit -m "SmartMercado"
   git remote add origin git@github.com:SEU_USUARIO/smartmercado.git
   git push -u origin main
   ```

4. **Projeto no Vercel**
   1. Em https://vercel.com/new, importe o repositório. O Vercel detecta o preset **FastAPI**. Deixe os campos *Build Command*
      e *Output Directory* vazios: a pasta `public/` vai para a CDN e o resto das rotas vai para o app FastAPI.
      Não crie *rewrites* para `/api`. O preset já repassa o caminho original, e um rewrite faria o FastAPI receber `/api/index` e responder 404.
   2. Em *Environment Variables*, cadastre (Production e Preview):
      | Variável | Valor |
      |---|---|
      | `APP_PIN` | seu PIN (use **6 dígitos ou mais**) |
      | `GEMINI_API_KEY` | chave do passo 2 |
      | `FIREBASE_CREDENTIALS` | o JSON em uma linha, do passo 1.3 |
      | `GEMINI_MODEL` | *(opcional)* padrão `gemini-3.8-flash` |
   3. Clique em **Deploy**.

   Pela CLI, o caminho equivalente é:
   ```bash
   npm i -g vercel
   vercel link
   vercel env add APP_PIN production
   vercel env add GEMINI_API_KEY production
   vercel env add FIREBASE_CREDENTIALS production
   vercel --prod
   ```

5. **Conferir**
   - O `vercel.json` fixa a função em **gru1** (São Paulo), perto da SEFAZ-GO e do Firestore, e define `maxDuration` de 60 s para a leitura por foto.
   - Abra `https://SEU-APP.vercel.app`, digite o PIN e escaneie uma nota.
   - Se algo falhar, veja os logs em *Vercel → Project → Logs*. Um 503 com "APP_PIN não configurado" ou
     "FIREBASE_CREDENTIALS ..." indica variável de ambiente faltando. Depois de mudar uma variável, faça um *Redeploy*.

6. **Instalar no celular**: abra o site no Chrome (Android) e use *Instalar app*, ou no Safari (iOS)
   use *Compartilhar → Adicionar à Tela de Início*.

## Segurança

- O PIN é obrigatório em todas as rotas `/api`, com comparação em tempo constante. Depois de 5 erros, o IP fica bloqueado por 5 minutos.
  Esse bloqueio fica na memória de cada instância serverless, então ajuda mas não garante nada: use um PIN longo.
- Só são aceitos links `https://nfeweb.sefaz.go.gov.br`. Isso vale também para cada redirecionamento, e a verificação de SSL fica ligada.
- O tamanho das respostas da SEFAZ e das imagens enviadas é limitado. O tipo da imagem é conferido pela assinatura do arquivo.
- O frontend nunca usa `innerHTML` com dados externos (só `textContent`, `value` e `createElement`), e há uma CSP restritiva.
- O CSV neutraliza células que começam com `=`, `+`, `-` ou `@`, para evitar injeção de fórmula.
- Os segredos ficam só em variáveis de ambiente, e `.env` e as credenciais estão no `.gitignore`.
- Chamadas externas só no backend e com host fixo: SEFAZ-GO (`nfeweb.sefaz.go.gov.br`, também nos redirecionamentos), BrasilAPI
  (`brasilapi.com.br`, sem redirecionamento, resposta limitada a 1 MB, prazo de 4 s) e Gemini. O CNPJ entra no caminho só como dígitos.
- XML enviado por você: sem `DOCTYPE`/`ENTITY`, UTF-8, até 2 MB. Importação de lançamentos: até 1 MB e 5000 linhas.
- A CSP continua `script-src 'self'`: gráficos em SVG próprio, sem biblioteca nem CDN de script. O Google Fonts só libera estilo e fonte.
- `dados/` (seus lançamentos) e `.claude/` ficam fora do git. Os fixtures de caixa dos testes (`tests/fixtures/caixa_exemplo.json`) são dados fictícios.

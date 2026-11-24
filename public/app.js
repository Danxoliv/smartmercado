"use strict";

/* SmartMercado — frontend sem framework.
 * Regra: dados externos só entram no DOM via textContent / value / createElement (nunca innerHTML). */

const HOST_NFCE = "nfeweb.sefaz.go.gov.br";
const LIMITE_FOTO = 1.5 * 1024 * 1024;
const TOLERANCIA = 0.05;
const CHAVE_PIN = "sm_pin";

const $ = (sel) => document.querySelector(sel);
const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const brlUnit = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL", minimumFractionDigits: 2, maximumFractionDigits: 3 });
const numBr = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 3 });
const dataBr = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short", timeZone: "America/Sao_Paulo" });
const diaBr = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeZone: "America/Sao_Paulo" });

let categorias = [];
let nota = null;       // nota em revisão
let editando = null;   // chave da compra já salva que está sendo editada (null = compra nova)
let scanner = null;    // instância do Html5Qrcode
let ocupado = false;

/* ---------- DOM seguro ---------- */
function h(tag, props = {}, ...filhos) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const f of filhos.flat()) {
    if (f == null || f === false) continue;
    el.append(f instanceof Node ? f : document.createTextNode(String(f)));
  }
  return el;
}

/* ---------- feedback ---------- */
let toastTimer;
function toast(msg, erro = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("erro-t", erro);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, erro ? 6000 : 3000);
}

function setOcupado(sim, msg = "Processando…") {
  ocupado = sim;
  $("#carregando").hidden = !sim;
  $("#carregando-msg").textContent = msg;
  document.querySelectorAll("#app button, #app input, #app select, #app label.btn").forEach((el) => {
    if (el.tagName === "LABEL") el.toggleAttribute("aria-disabled", sim);
    else el.disabled = sim;
  });
}

async function comBloqueio(msg, fn) {
  if (ocupado) return;
  setOcupado(true, msg);
  try { return await fn(); }
  catch (e) { toast(e.message || "Erro inesperado.", true); }
  finally { setOcupado(false); }
}

/* ---------- API ---------- */
class ErroApi extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

function detalheErro(corpo, status) {
  const d = corpo && corpo.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d.map((e) => `${(e.loc || []).slice(1).join(" › ")}: ${e.msg}`).join("\n");
  }
  return `Erro ${status}.`;
}

async function api(caminho, opcoes = {}) {
  const pin = localStorage.getItem(CHAVE_PIN) || "";
  const headers = { "X-App-Pin": pin, ...(opcoes.headers || {}) };
  let corpo = opcoes.body;
  if (opcoes.json !== undefined) {
    headers["Content-Type"] = "application/json";
    corpo = JSON.stringify(opcoes.json);
  }
  let resp;
  try {
    resp = await fetch(caminho, { method: opcoes.method || "GET", headers, body: corpo, cache: "no-store" });
  } catch {
    throw new ErroApi("Sem conexão com o servidor.", 0);
  }
  if (resp.status === 401) {
    sair();
    throw new ErroApi("PIN inválido. Entre novamente.", 401);
  }
  if (opcoes.blob) {
    if (!resp.ok) throw new ErroApi(`Erro ${resp.status}.`, resp.status);
    return resp;
  }
  const dados = await resp.json().catch(() => null);
  if (!resp.ok) throw new ErroApi(detalheErro(dados, resp.status), resp.status);
  return dados;
}

/* ---------- PIN ---------- */
function sair() {
  localStorage.removeItem(CHAVE_PIN);
  pararScanner();
  $("#app").hidden = true;
  $("#tela-pin").hidden = false;
  $("#pin").value = "";
  $("#pin").focus();
}

async function entrar(pin) {
  localStorage.setItem(CHAVE_PIN, pin);
  try {
    await api("/api/ping");
    categorias = await api("/api/categorias");
    $("#dcat").replaceChildren(h("option", { value: "", text: "Todas as categorias" }), ...categorias.map((c) => h("option", { value: c, text: c })));
    $("#tela-pin").hidden = true;
    $("#app").hidden = false;
    $("#pin-erro").textContent = "";
  } catch (e) {
    localStorage.removeItem(CHAVE_PIN);
    $("#tela-pin").hidden = false;
    $("#pin-erro").textContent = e.message;
  }
}

/* ---------- abas ---------- */
const TITULOS = { escanear: "Scanner", revisao: "Revisar compra", painel: "Início", caixa: "Caixa", dados: "Compras salvas", comparar: "Comparar preços" };

function mostrarAba(nome) {
  if (nome !== "escanear") pararScanner();
  document.querySelectorAll(".aba").forEach((s) => s.classList.toggle("ativa", s.id === `aba-${nome}`));
  document.querySelectorAll(".nav button").forEach((b) => b.classList.toggle("ativo", b.dataset.aba === nome));
  $("#titulo").textContent = TITULOS[nome];
  $(".nav").hidden = nome === "revisao";
  window.scrollTo(0, 0);
  if (nome === "painel") carregarPainel();
  if (nome === "dados") carregarDados();
  if (nome === "caixa") carregarCaixa();
}

/* ---------- leitura por QR Code ---------- */
function linkValido(texto) {
  try {
    const u = new URL(texto.trim());
    return u.protocol === "https:" && u.hostname === HOST_NFCE && !u.username && !u.password;
  } catch { return false; }
}

async function iniciarScanner() {
  if (typeof Html5Qrcode === "undefined") return toast("Leitor de QR Code não carregou.", true);
  $("#leitor").hidden = false;
  $("#btn-scan").hidden = true;
  $("#btn-scan-parar").hidden = false;
  const aoLer = async (texto) => {
    await pararScanner();
    if (!linkValido(texto)) return toast("Esse QR Code não é de uma NFC-e de Goiás (nfeweb.sefaz.go.gov.br, HTTPS).", true);
    lerLink(texto);
  };
  const base = { fps: 10, qrbox: (w, h) => { const l = Math.floor(Math.min(w, h) * 0.75); return { width: l, height: l }; } };
  // QR pequeno de cupom borra com a configuração padrão: pede resolução alta, foco contínuo e o leitor nativo do navegador.
  const nitida = {
    ...base,
    videoConstraints: { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 }, advanced: [{ focusMode: "continuous" }] },
    experimentalFeatures: { useBarCodeDetectorIfSupported: true },
  };
  for (const [i, config] of [nitida, base].entries()) {  // se o celular recusar a configuração nítida, tenta a simples
    scanner = new Html5Qrcode("leitor", { verbose: false });
    try {
      await scanner.start({ facingMode: "environment" }, config, aoLer, () => {});
      try { await scanner.applyVideoConstraints({ advanced: [{ focusMode: "continuous" }] }); } catch { /* sem controle de foco: segue */ }
      return;
    } catch (e) {
      try { scanner.clear(); } catch { /* nada a limpar */ }
      scanner = null;
      if (i === 1) {
        await pararScanner();
        toast("Não foi possível abrir a câmera. Verifique a permissão ou use \"Ler QR de uma foto\".", true);
      }
    }
  }
}

/** Plano B quando a câmera ao vivo não foca: a câmera nativa tira a foto e o QR é lido dela. */
async function lerQrDeFoto(arquivo) {
  const texto = await comBloqueio("Lendo o QR Code…", async () => {
    const reduzida = new File([await comprimir(arquivo)], "qr.jpg", { type: "image/jpeg" });
    const leitor = new Html5Qrcode("leitor-oculto", { verbose: false });
    try {
      return await leitor.scanFile(reduzida, false);
    } catch {
      throw new Error("Não achei um QR Code nessa foto. Chegue mais perto (uns 15 cm), com boa luz, e tente de novo.");
    } finally {
      try { leitor.clear(); } catch { /* nada a limpar */ }
    }
  });
  if (!texto) return;
  if (!linkValido(texto)) return toast("Esse QR Code não é de uma NFC-e de Goiás (nfeweb.sefaz.go.gov.br, HTTPS).", true);
  lerLink(texto);
}

async function pararScanner() {
  const s = scanner;
  scanner = null;
  if (s) {
    try { if (s.isScanning) await s.stop(); s.clear(); } catch { /* já parado */ }
  }
  $("#leitor").hidden = true;
  $("#btn-scan").hidden = false;
  $("#btn-scan-parar").hidden = true;
}

function lerLink(url) {
  return comBloqueio("Consultando a SEFAZ-GO…", async () => {
    const dados = await api("/api/nfce/link", { method: "POST", json: { url } });
    abrirRevisao(dados);
    return true;
  });
}

/* ---------- leitura por foto ---------- */
function canvasParaBlob(canvas, qualidade) {
  return new Promise((ok) => canvas.toBlob(ok, "image/jpeg", qualidade));
}

async function comprimir(arquivo) {
  const img = await createImageBitmap(arquivo, { imageOrientation: "from-image" });
  let escala = Math.min(1, 2400 / Math.max(img.width, img.height));
  let qualidade = 0.85;
  for (let i = 0; i < 12; i++) {
    const c = document.createElement("canvas");
    c.width = Math.round(img.width * escala);
    c.height = Math.round(img.height * escala);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    const blob = await canvasParaBlob(c, qualidade);
    if (blob && blob.size < LIMITE_FOTO) { img.close(); return blob; }
    if (qualidade > 0.55) qualidade -= 0.1; else escala *= 0.85;
  }
  img.close();
  throw new Error("Não consegui reduzir a foto para menos de 1,5 MB.");
}

function lerFoto(arquivo) {
  return comBloqueio("Lendo a foto com IA…", async () => {
    const blob = await comprimir(arquivo);
    const form = new FormData();
    form.append("foto", blob, "nota.jpg");
    const dados = await api("/api/nfce/foto", { method: "POST", body: form });
    abrirRevisao(dados);
  });
}

function lerXml(arquivo) {
  return comBloqueio("Lendo o XML…", async () => {
    const form = new FormData();
    form.append("xml", arquivo, arquivo.name);
    abrirRevisao(await api("/api/nfce/xml", { method: "POST", body: form }));
  });
}

/* ---------- revisão ---------- */
const arred = (v) => Math.round(v * 100) / 100;

function paraDatetimeLocal(iso) {
  // ISO já vem no horário de Brasília (ex.: 2026-10-09T13:45:12-03:00)
  return iso ? iso.slice(0, 16) : "";
}

function agoraLocal() {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

function abrirRevisao(dados, chaveSalva = null) {
  nota = structuredClone(dados);
  editando = chaveSalva;
  $("#rev-mercado").value = nota.estabelecimento || nota.mercado || "";
  $("#rev-data").value = paraDatetimeLocal(nota.data) || agoraLocal(); // cupom não fiscal costuma não ter data
  $("#rev-total").value = (nota.total ?? 0).toFixed(2);
  $("#rev-chave").value = nota.chave || "";
  $("#rev-chave").readOnly = !!editando;
  $("#btn-salvar").textContent = editando ? "Salvar alterações" : "Salvar compra";
  renderItens();
  mostrarAba("revisao");
  $("#titulo").textContent = editando ? "Editar compra" : TITULOS.revisao;
}

function opcoesCategoria(atual) {
  const lista = categorias.includes(atual) ? categorias : [...categorias, atual];
  return lista.map((c) => h("option", { value: c, text: c, selected: c === atual }));
}

function renderItens() {
  const ul = $("#rev-itens");
  ul.replaceChildren(...nota.itens.map((it, idx) => {
    const id = `it${idx}`;
    const num = (rotulo, campo, passo, aoMudar) => h("div", {},
      h("label", { for: `${id}${campo}`, text: rotulo }),
      h("input", {
        id: `${id}${campo}`, type: "number", inputmode: "decimal", step: passo, min: "0", value: String(it[campo]),
        oninput: (e) => {
          const v = parseFloat(e.target.value);
          if (!(v >= 0)) return;
          it[campo] = v;
          aoMudar();
          // editando uma compra salva, o valor pago acompanha os itens
          if (editando) $("#rev-total").value = arred(nota.itens.reduce((t, i) => t + i.preco_total, 0) - (nota.descontos || 0)).toFixed(2);
          atualizarResumo();
        },
      }),
    );
    // qtd e preço unitário recalculam o total; o total também pode ser corrigido direto (ex.: peso arredondado)
    const recalcular = () => {
      it.preco_total = arred(it.preco_unit * it.qtd);
      $(`#${id}preco_total`).value = String(it.preco_total);
    };
    return h("li", { class: "item" },
      h("div", { class: "item-topo" },
        h("input", {
          type: "text", value: it.nome, maxlength: "200", "aria-label": "Nome do produto",
          oninput: (e) => { it.nome = e.target.value; },
        }),
        h("button", {
          type: "button", class: "item-rem", "aria-label": `Remover ${it.nome}`, text: "✕",
          onclick: () => { nota.itens.splice(idx, 1); renderItens(); },
        }),
      ),
      h("select", { "aria-label": "Categoria", onchange: (e) => { it.categoria = e.target.value; it.categoria_manual = true; } }, opcoesCategoria(it.categoria)),
      h("div", { class: "item-grade" },
        num(`Qtd (${it.unidade})`, "qtd", "any", recalcular),
        num("Unit. (R$)", "preco_unit", "any", recalcular),
        num("Total (R$)", "preco_total", "0.01", () => {}),
      ),
      (it.ean || it.codigo) ? h("div", { class: "item-cod", text: it.ean ? `EAN ${it.ean}` : `Cód. ${it.codigo}` }) : null,
    );
  }));
  atualizarResumo();
}

function atualizarResumo() {
  const soma = arred(nota.itens.reduce((s, i) => s + i.preco_total, 0));
  const descontos = nota.descontos || 0;
  const total = parseFloat($("#rev-total").value) || 0;
  const dif = arred(soma - descontos - total);
  const partes = [`${nota.itens.length} itens`, `soma ${brl.format(soma)}`];
  if (descontos) partes.push(`descontos ${brl.format(descontos)}`);
  if (!editando && !nota.chave) partes.push("sem chave fiscal: o app cria um ID");
  if (!editando && !nota.data) partes.push("sem data no cupom: usei agora");
  $("#rev-resumo").textContent = partes.join(" · ");
  const aviso = $("#rev-aviso");
  if (!nota.itens.length) {
    aviso.textContent = "Nenhum item na compra.";
    aviso.hidden = false;
  } else if (Math.abs(dif) > TOLERANCIA) {
    aviso.textContent = `Atenção: a soma dos itens${descontos ? " menos os descontos" : ""} (${brl.format(soma - descontos)}) não bate com o valor pago (${brl.format(total)}). Diferença de ${brl.format(Math.abs(dif))}.`;
    aviso.hidden = false;
  } else {
    aviso.hidden = true;
  }
}

function fecharRevisao(destino) {
  nota = null;
  editando = null;
  $("#rev-itens").replaceChildren();
  mostrarAba(destino);
}

function cancelarRevisao() {
  if (nota && !confirm(editando ? "Descartar as alterações?" : "Descartar esta compra?")) return;
  fecharRevisao(editando ? "dados" : "escanear");
}

function salvar() {
  if (!nota) return;
  const corpo = {
    chave: $("#rev-chave").value.replace(/\D/g, ""),
    estabelecimento: $("#rev-mercado").value.trim(),
    tipo_estabelecimento: nota.tipo_estabelecimento || null,
    cnpj: nota.cnpj || null,
    data: $("#rev-data").value ? `${$("#rev-data").value}:00` : null,
    total: parseFloat($("#rev-total").value),
    descontos: nota.descontos || 0,
    origem: nota.origem,
    itens: nota.itens.map((i) => ({
      nome: i.nome.trim(), codigo: i.codigo || null, ean: i.ean || null, qtd: i.qtd,
      unidade: i.unidade || "UN", preco_unit: i.preco_unit, preco_total: i.preco_total, categoria: i.categoria,
      categoria_manual: !!i.categoria_manual,
    })),
  };
  if (corpo.chave && corpo.chave.length !== 44) return toast("A chave precisa ter 44 dígitos, ou ficar vazia se o cupom não for fiscal.", true);
  if (!corpo.estabelecimento || !corpo.data || !(corpo.total >= 0)) return toast("Preencha estabelecimento, data e valor pago.", true);
  if (!corpo.itens.length) return toast("A compra precisa de ao menos um item.", true);
  if (corpo.itens.some((i) => !i.nome)) return toast("Há item sem nome.", true);

  const alterando = editando;
  let novoId = null;
  return comBloqueio("Salvando…", async () => {
    try {
      if (alterando) await api(`/api/compras/${encodeURIComponent(alterando)}`, { method: "PUT", json: corpo });
      else novoId = (await api("/api/compras", { method: "POST", json: corpo })).id;
    } catch (e) {
      if (e.status === 409) throw new Error("Esta nota já foi cadastrada antes.");
      throw e;
    }
    return true;
  }).then((ok) => {
    if (!ok) return;
    toast(alterando ? "Compra atualizada!" : "Compra salva!");
    fecharRevisao(alterando ? "dados" : "painel");
    if (novoId) sugerirLancamento(novoId); // oferece lançar no caixa (diário ou cartão)
  });
}

/* ---------- painel ---------- */
function mesAtual() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function barras(ul, linhas, rotulo) {
  if (!linhas.length) return ul.replaceChildren(h("li", { class: "vazio", text: "Sem dados no mês." }));
  const max = Math.max(...linhas.map((l) => l.total)) || 1;
  ul.replaceChildren(...linhas.map((l) => h("li", {},
    h("div", { class: "rotulo" }, h("span", { text: rotulo(l) }), h("strong", { text: brl.format(l.total) })),
    h("div", { class: "trilho" }, h("div", { class: "preench", style: { width: `${(l.total / max) * 100}%` } })),
  )));
}

const reaisCurto = (v) => (v >= 1000 ? `${numBr.format(Math.round(v / 100) / 10)} mil` : numBr.format(Math.round(v)));

function renderComparativo(d) {
  const pill = $("#p-var");
  pill.hidden = d.variacao_pct == null;
  if (!pill.hidden) pill.textContent = `${d.variacao_pct > 0 ? "+" : ""}${numBr.format(d.variacao_pct)}% vs. mês anterior`;
  const max = Math.max(...d.meses.map((m) => m.total)) || 1;
  $("#p-evol").replaceChildren(...d.meses.map((m, i) => {
    const [a, mm] = m.mes.split("-");
    const nome = new Date(+a, +mm - 1, 1).toLocaleDateString("pt-BR", { month: "short" }).replace(".", "");
    return h("div", { class: i === d.meses.length - 1 ? "col atual" : "col", title: `${nome}/${a}: ${brl.format(m.total)} em ${m.compras} compras` },
      h("span", { class: "val", text: m.total ? reaisCurto(m.total) : "–" }),
      h("div", { class: "bar", style: { height: `${(m.total / max) * 100}%` } }),
      h("span", { text: nome }),
    );
  }));
  const maxC = Math.max(...d.categorias.map((c) => Math.max(c.atual, c.anterior))) || 1;
  const ul = $("#p-comp");
  if (!d.categorias.length) return ul.replaceChildren(h("li", { class: "vazio", text: "Sem dados nos dois meses." }));
  ul.replaceChildren(...d.categorias.map((c) => h("li", {},
    h("div", { class: "rotulo" }, h("span", { text: c.categoria }), h("strong", { text: brl.format(c.atual) })),
    h("div", { class: "trilho" }, h("div", { class: "preench", style: { width: `${(c.atual / maxC) * 100}%` } })),
    h("div", { class: "trilho ant" }, h("div", { class: "preench", style: { width: `${(c.anterior / maxC) * 100}%` } })),
    h("span", { class: "ant-txt", text: `mês anterior ${brl.format(c.anterior)}` }),
  )));
}

function renderCategorias(linhas) {
  const ul = $("#p-cat");
  if (!linhas.length) {
    $("#p-donut").replaceChildren();
    return ul.replaceChildren(h("li", { class: "vazio", text: "Sem dados no mês." }));
  }
  const cores = rosca($("#p-donut"), linhas.map((l) => ({ rotulo: l.categoria, valor: l.total })), "Total do mês", brl.format);
  const total = cores.reduce((s, c) => s + c.valor, 0) || 1;
  ul.replaceChildren(...cores.map((c) => h("li", {},
    h("i", { class: "ponto", style: { background: c.cor } }),
    h("span", { class: "nome", text: c.rotulo }),
    h("span", { class: "pct", text: `${numBr.format(Math.round((c.valor / total) * 1000) / 10)}%` }),
    h("strong", { text: brl.format(c.valor) }),
  )));
}

async function carregarPainel() {
  const mes = $("#mes").value || mesAtual();
  $("#mes").value = mes;
  await comBloqueio("Carregando painel…", async () => {
    const [p, comp] = await Promise.all([
      api(`/api/painel?mes=${encodeURIComponent(mes)}`),
      api(`/api/comparativo?mes=${encodeURIComponent(mes)}`),
    ]);
    renderComparativo(comp);
    $("#p-total").textContent = brl.format(p.total);
    $("#p-qtd").textContent = `${p.qtd_compras} ${p.qtd_compras === 1 ? "compra" : "compras"}`;
    renderCategorias(p.por_categoria);
    barras($("#p-mercado"), p.por_mercado, (l) => `${l.mercado} (${l.compras})`);
    const ul = $("#p-compras");
    if (!p.compras.length) return ul.replaceChildren(h("li", { class: "vazio", text: "Nenhuma compra neste mês." }));
    ul.replaceChildren(...p.compras.map((c) => h("li", {},
      h("div", { class: "info" },
        h("strong", { text: c.mercado }),
        h("span", { class: "sub", text: `${dataBr.format(new Date(c.data))} · ${c.qtd_itens} itens · ${{ foto: "foto", xml: "XML" }[c.origem] || "QR Code"}` }),
      ),
      h("span", { class: "valor", text: brl.format(c.total) }),
      h("button", {
        type: "button", class: "item-rem", "aria-label": `Excluir compra de ${c.mercado}`, text: "✕",
        onclick: () => excluirCompra(c),
      }),
    )));
  });
}

function excluirCompra(c) {
  if (!confirm(`Excluir a compra de ${c.mercado} (${brl.format(c.total)})?`)) return;
  comBloqueio("Excluindo…", async () => {
    await api(`/api/compras/${encodeURIComponent(c.chave)}`, { method: "DELETE" });
    toast("Compra excluída.");
  }).then(carregarPainel);
}

function baixarCsv(mes) {
  return comBloqueio("Gerando CSV…", async () => {
    const resp = await api(`/api/export.csv${mes ? `?mes=${encodeURIComponent(mes)}` : ""}`, { blob: true });
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: mes ? `smartmercado-${mes}.csv` : "smartmercado-tudo.csv" });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
}

/* ---------- dados: tabela de consulta ---------- */
const LIMITE_LINHAS = 500;
let comprasCarregadas = [];
const semAcento = (s) => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

async function carregarDados() {
  if (!$("#dmes").dataset.iniciado) { $("#dmes").value = mesAtual(); $("#dmes").dataset.iniciado = "1"; }
  const mes = $("#dmes").value; // vazio = todas as compras
  await comBloqueio("Carregando compras…", async () => {
    comprasCarregadas = (await api(`/api/compras${mes ? `?mes=${encodeURIComponent(mes)}` : ""}`)).compras;
    renderDados();
  });
}

function renderDados() {
  const filtro = semAcento($("#dfiltro").value.trim()), cat = $("#dcat").value;
  const linhas = comprasCarregadas.flatMap((c) => c.itens.map((i) => ({ c, i })))
    .filter(({ c, i }) => (!cat || i.categoria === cat) && (!filtro || semAcento(`${i.nome} ${c.estabelecimento || c.mercado}`).includes(filtro)));
  $("#d-resumo").textContent = `${comprasCarregadas.length} ${comprasCarregadas.length === 1 ? "compra" : "compras"} · ${linhas.length} ${linhas.length === 1 ? "item" : "itens"}` +
    (linhas.length > LIMITE_LINHAS ? ` (mostrando ${LIMITE_LINHAS}; use o filtro)` : "") +
    ($("#dmes").value ? "" : " · todos os meses");
  $("#d-corpo").replaceChildren(...linhas.slice(0, LIMITE_LINHAS).map(({ c, i }) => h("tr", {},
    h("td", {},
      h("button", { type: "button", class: "link", text: i.nome, onclick: () => abrirRevisao(c, c.chave) }),
      h("span", { class: "sub", text: `${diaBr.format(new Date(c.data))} · ${c.estabelecimento || c.mercado}` }),
      h("span", { class: "etiqueta", text: i.categoria }),
    ),
    h("td", { class: "num", text: `${numBr.format(i.qtd)} ${i.unidade}` }),
    h("td", { class: "num", text: brlUnit.format(i.preco_unit) }),
    h("td", { class: "num", text: brl.format(i.preco_total) }),
  )));
}

/* ---------- comparador ---------- */
function buscar(q) {
  return comBloqueio("Buscando…", async () => {
    const { produtos } = await api(`/api/comparar?q=${encodeURIComponent(q)}`);
    const alvo = $("#resultados");
    if (!produtos.length) return alvo.replaceChildren(h("p", { class: "vazio", text: "Nenhum produto encontrado." }));
    alvo.replaceChildren(...produtos.map(cardProduto));
  });
}

function cardProduto(p) {
  const menor = Math.min(...p.por_mercado.map((m) => m.ultimo_preco));
  const tabMercados = h("table", { class: "tabela" },
    h("thead", {}, h("tr", {},
      h("th", { text: "Mercado" }), h("th", { class: "num", text: "Último" }),
      h("th", { class: "num", text: "Mín." }), h("th", { class: "num", text: "Em" }),
    )),
    h("tbody", {}, p.por_mercado.map((m) => h("tr", {},
      h("td", { text: m.mercado }),
      h("td", { class: m.ultimo_preco === menor && p.por_mercado.length > 1 ? "num melhor" : "num", text: brlUnit.format(m.ultimo_preco) }),
      h("td", { class: "num", text: brlUnit.format(m.menor_preco) }),
      h("td", { class: "num", text: diaBr.format(new Date(m.data)) }),
    ))),
  );
  const tabHist = h("table", { class: "tabela" },
    h("thead", {}, h("tr", {},
      h("th", { text: "Data" }), h("th", { text: "Mercado" }), h("th", { class: "num", text: "Preço" }),
    )),
    h("tbody", {}, p.historico.map((x) => h("tr", {},
      h("td", { text: diaBr.format(new Date(x.data)) }),
      h("td", { text: x.mercado }),
      h("td", { class: "num", text: `${brlUnit.format(x.preco_unit)}/${x.unidade}` }),
    ))),
  );
  return h("div", { class: "card" },
    h("h2", { text: p.nome }),
    h("p", { class: "sub", text: `${p.categoria ? `${p.categoria} · ` : ""}${p.ean ? `EAN ${p.ean} · ` : ""}${p.historico.length} ${p.historico.length === 1 ? "compra" : "compras"}` }),
    tabMercados,
    h("details", {}, h("summary", { class: "sub", text: "Histórico" }), tabHist),
  );
}

/* ---------- início ---------- */
function ligarEventos() {
  $("#form-pin").addEventListener("submit", (e) => {
    e.preventDefault();
    const pin = $("#pin").value.trim();
    if (pin) entrar(pin);
  });
  $("#btn-sair").addEventListener("click", () => { if (!nota || confirm("Descartar a compra em revisão?")) { nota = null; editando = null; sair(); } });
  document.querySelectorAll(".nav button").forEach((b) => b.addEventListener("click", () => mostrarAba(b.dataset.aba)));

  $("#btn-scan").addEventListener("click", iniciarScanner);
  $("#btn-scan-parar").addEventListener("click", pararScanner);
  $("#foto-qr").addEventListener("change", (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (f) lerQrDeFoto(f);
  });
  $("#foto").addEventListener("change", (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (f) lerFoto(f);
  });
  $("#xml").addEventListener("change", (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (f) lerXml(f);
  });
  $("#form-link").addEventListener("submit", (e) => {
    e.preventDefault();
    const url = $("#link").value.trim();
    if (!linkValido(url)) return toast("Use um link https://nfeweb.sefaz.go.gov.br/…", true);
    lerLink(url).then((ok) => { if (ok) $("#link").value = ""; });
  });

  $("#rev-total").addEventListener("input", atualizarResumo);
  $("#btn-cancelar").addEventListener("click", cancelarRevisao);
  $("#btn-salvar").addEventListener("click", salvar);

  $("#mes").addEventListener("change", carregarPainel);
  $("#dmes").addEventListener("change", carregarDados);
  $("#dfiltro").addEventListener("input", renderDados);
  $("#dcat").addEventListener("change", renderDados);
  $("#btn-csv-mes").addEventListener("click", () => baixarCsv($("#mes").value || mesAtual()));
  $("#btn-csv-tudo").addEventListener("click", () => baixarCsv(null));

  $("#form-busca").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#busca").value.trim();
    if (q.length >= 2) buscar(q);
  });

  window.addEventListener("beforeunload", (e) => { if (nota) e.preventDefault(); });
}

document.addEventListener("DOMContentLoaded", () => {
  ligarEventos();
  const pin = localStorage.getItem(CHAVE_PIN);
  if (pin) entrar(pin); else sair();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
});

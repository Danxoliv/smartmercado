"use strict";

/* Caixa mensal: saldos, projeção de 12 meses, parcelas e lançamentos.
 * Usa h(), api(), brl, comBloqueio(), toast() de app.js e os gráficos de graficos.js. Texto sempre via textContent. */

let cxSub = "saldos";
let cxProj = null;     // resposta de /api/caixa/projecao
let cxLancs = [];      // lançamentos (com id)
let cxParcelas = [];
let cxEditando = null; // id do lançamento em edição

const TIPOS_CX = { entrada: "Entrada", saida: "Saída", diario: "Diário", economia: "Economia", cartao: "Cartão" };
const RECORRENCIAS = { unica: "Única", diaria: "Todo dia", semanal: "Toda semana", mensal: "Todo mês", anual: "Todo ano" };
const mesNome = (m) => new Date(+m.slice(0, 4), +m.slice(5, 7) - 1, 1).toLocaleDateString("pt-BR", { month: "short", year: "2-digit" }).replace(".", "");
const diaMes = (iso) => `${iso.slice(8, 10)}/${iso.slice(5, 7)}`;
const brlCurto = (v) => `${v < 0 ? "−" : ""}R$ ${numBr.format(Math.round(Math.abs(v)))}`;

/* ---------- carga ---------- */
function carregarCaixa() {
  return comBloqueio("Carregando caixa…", async () => {
    const [proj, lancs, parc, cfg] = await Promise.all([
      api("/api/caixa/projecao?meses=12"), api("/api/caixa/lancamentos"), api("/api/caixa/parcelas"), api("/api/caixa/config"),
    ]);
    cxProj = proj;
    cxLancs = lancs.lancamentos;
    cxParcelas = parc.parcelas;
    $("#cx-saldo").value = cfg.saldo_inicial ?? 0;
    $("#cx-data-saldo").value = cfg.data_saldo || proj.inicio;
    const sel = $("#cx-mes");
    if (!sel.options.length || sel.dataset.inicio !== proj.inicio) {
      sel.replaceChildren(...proj.meses.map((m) => h("option", { value: m.mes, text: mesNome(m.mes) })));
      sel.dataset.inicio = proj.inicio;
    }
    renderCaixa();
  });
}

function mostrarSub(nome) {
  cxSub = nome;
  document.querySelectorAll(".segmentos button").forEach((b) => b.classList.toggle("ativo", b.dataset.sub === nome));
  document.querySelectorAll(".sub-aba").forEach((s) => s.classList.toggle("ativa", s.id === `cx-${nome}`));
}

function renderCaixa() {
  mostrarSub(cxSub);
  renderSaldos();
  renderProjecao();
  renderParcelas();
  renderLancamentos();
}

const kpi = (rotulo, valor, classe = "") => h("div", { class: "kpi" }, h("span", { class: "sub", text: rotulo }), h("strong", { class: classe, text: valor }));

/* ---------- saldos do mês ---------- */
function renderSaldos() {
  if (!cxProj) return;
  const sel = $("#cx-mes");
  const m = cxProj.meses.find((x) => x.mes === sel.value) || cxProj.meses[0];
  sel.value = m.mes;
  $("#cx-kpis").replaceChildren(
    kpi("Saldo final", brl.format(m.saldo_final), m.saldo_final < 0 ? "neg" : ""),
    kpi(m.performance < 0 ? "Falta no mês" : "Sobra no mês", brl.format(m.performance), m.performance < 0 ? "neg" : "pos"),
    kpi("Custo de vida", brl.format(m.custo_vida)),
    kpi("Pior dia", `${brl.format(m.saldo_minimo)} · ${diaMes(m.dia_minimo)}`, m.saldo_minimo < 0 ? "neg" : ""),
  );
  $("#cx-tabela-mes").replaceChildren(...[
    ["Saldo no início", m.saldo_inicial], ["+ Entradas", m.entradas], ["− Saídas", m.saidas], ["− Diários", m.diarios],
    ["− Cartão", m.cartao], ["− Economias", m.economias],
  ].map(([r, v]) => h("tr", {}, h("td", { text: r }), h("td", { class: "num", text: brl.format(v) }))));
  const dias = cxProj.dias.filter((d) => d.data.startsWith(m.mes));
  linhaSaldo($("#cx-linha"), dias.map((d) => ({ rotulo: diaMes(d.data), valor: d.saldo })), brl.format,
    `Saldo diário em ${mesNome(m.mes)}; menor saldo ${brl.format(m.saldo_minimo)} em ${diaMes(m.dia_minimo)}`);
}

/* ---------- projeção ---------- */
function renderProjecao() {
  if (!cxProj) return;
  const ms = cxProj.meses;
  barrasZero($("#cx-barras"), ms.map((m, i) => ({ rotulo: mesNome(m.mes).slice(0, 3), valor: m.performance, destaque: i === 0 })), brl.format,
    "Sobra ou falta por mês nos próximos 12 meses");
  linhaSaldo($("#cx-linha-12"), cxProj.dias.filter((_, i) => i % 3 === 0).map((d) => ({ rotulo: mesNome(d.data.slice(0, 7)), valor: d.saldo })), brl.format,
    `Saldo diário em 12 meses; final ${brl.format(cxProj.saldo_final)}`, 4);
  $("#cx-resumo-12").textContent = `Saldo de ${brl.format(cxProj.saldo_inicial)} em ${diaMes(cxProj.inicio)} → ${brl.format(cxProj.saldo_final)} em ${diaMes(cxProj.fim)}/${cxProj.fim.slice(2, 4)} · resultado ${brl.format(cxProj.performance_total)}`;
  $("#cx-tabela-12").replaceChildren(...ms.map((m) => h("tr", {},
    h("td", { text: mesNome(m.mes) }),
    h("td", { class: "num", text: brlCurto(m.entradas) }),
    h("td", { class: "num", text: brlCurto(m.custo_vida) }),
    h("td", { class: m.performance < 0 ? "num neg" : "num pos", text: brlCurto(m.performance) }),
    h("td", { class: m.saldo_final < 0 ? "num neg" : "num", text: brlCurto(m.saldo_final) }),
  )));
}

/* ---------- parcelas ---------- */
function renderParcelas() {
  const ul = $("#cx-parcelas-lista");
  if (!cxParcelas.length) return ul.replaceChildren(h("li", { class: "vazio", text: "Nenhum parcelamento cadastrado." }));
  const restante = cxParcelas.reduce((s, p) => s + p.total_restante, 0);
  $("#cx-parcelas-total").textContent = `Ainda a pagar em parcelas: ${brl.format(restante)}`;
  ul.replaceChildren(...cxParcelas.map((p) => {
    const pagas = p.parcelas_total - p.restantes;
    return h("li", { class: "parcela" },
      h("div", { class: "rotulo" }, h("span", { text: p.nome }), h("strong", { text: brl.format(p.valor) })),
      h("div", { class: "trilho" }, h("div", { class: "preench", style: { width: `${(pagas / p.parcelas_total) * 100}%` } })),
      h("span", { class: "sub", text: p.restantes
        ? `${pagas}/${p.parcelas_total} pagas · próxima ${diaMes(p.proxima)} · termina em ${diaMes(p.termina_em)}/${p.termina_em.slice(0, 4)} · faltam ${brl.format(p.total_restante)}`
        : `${p.parcelas_total}/${p.parcelas_total} · quitado` }),
    );
  }));
}

/* ---------- lançamentos ---------- */
function renderLancamentos() {
  const ul = $("#cx-lista");
  if (!cxLancs.length) return ul.replaceChildren(h("li", { class: "vazio", text: "Nenhum lançamento. Use o formulário ou importe um arquivo." }));
  ul.replaceChildren(...cxLancs.map((l) => h("li", {},
    h("div", { class: "info" },
      h("strong", { text: l.nome }),
      h("span", { class: "sub", text: `${TIPOS_CX[l.tipo]} · ${diaMes(l.data)}/${l.data.slice(0, 4)} · ${RECORRENCIAS[l.recorrencia]}` +
        (l.parcelas_total ? ` · ${l.parcela_atual}/${l.parcelas_total}` : "") + (l.origem === "importado" ? " · importado" : l.origem === "nota" ? " · nota" : "") }),
    ),
    h("span", { class: l.tipo === "entrada" ? "valor pos" : "valor", text: brl.format(l.valor) }),
    h("button", { type: "button", class: "item-rem", "aria-label": `Editar ${l.nome}`, text: "✎", onclick: () => editarLancamento(l) }),
    h("button", { type: "button", class: "item-rem", "aria-label": `Excluir ${l.nome}`, text: "✕", onclick: () => excluirLancamento(l) }),
  )));
}

function editarLancamento(l) {
  cxEditando = l.id;
  const f = $("#form-lanc");
  f.tipo.value = l.tipo; f.nome.value = l.nome; f.valor.value = l.valor; f.data.value = l.data;
  f.recorrencia.value = l.recorrencia; f.parcelas_total.value = l.parcelas_total ?? ""; f.parcela_atual.value = l.parcela_atual ?? "";
  $("#btn-lanc").textContent = "Salvar alterações";
  $("#btn-lanc-cancelar").hidden = false;
  f.scrollIntoView({ behavior: "smooth", block: "center" });
}

function limparFormLanc() {
  cxEditando = null;
  $("#form-lanc").reset();
  $("#form-lanc").data.value = new Date().toISOString().slice(0, 10);
  $("#btn-lanc").textContent = "Adicionar";
  $("#btn-lanc-cancelar").hidden = true;
}

function corpoLancamento() {
  const f = $("#form-lanc");
  const c = { tipo: f.tipo.value, nome: f.nome.value.trim(), valor: parseFloat(f.valor.value), data: f.data.value, recorrencia: f.recorrencia.value };
  if (f.parcelas_total.value) { c.parcelas_total = parseInt(f.parcelas_total.value, 10); if (f.parcela_atual.value) c.parcela_atual = parseInt(f.parcela_atual.value, 10); }
  return c;
}

function salvarLancamento(e) {
  e.preventDefault();
  const c = corpoLancamento();
  if (!c.nome || !(c.valor > 0) || !c.data) return toast("Preencha nome, valor e data.", true);
  const id = cxEditando;
  return comBloqueio("Salvando…", async () => {
    await api(id ? `/api/caixa/lancamentos/${encodeURIComponent(id)}` : "/api/caixa/lancamentos", { method: id ? "PUT" : "POST", json: c });
    return true;
  }).then((ok) => {
    if (!ok) return;
    toast(id ? "Lançamento atualizado." : "Lançamento adicionado.");
    limparFormLanc();
    carregarCaixa();
  });
}

function excluirLancamento(l) {
  if (!confirm(`Excluir "${l.nome}" (${brl.format(l.valor)})?`)) return;
  comBloqueio("Excluindo…", () => api(`/api/caixa/lancamentos/${encodeURIComponent(l.id)}`, { method: "DELETE" })).then(carregarCaixa);
}

function salvarConfigCaixa(e) {
  e.preventDefault();
  const saldo = parseFloat($("#cx-saldo").value), data = $("#cx-data-saldo").value;
  if (!Number.isFinite(saldo) || !data) return toast("Informe o saldo e a data.", true);
  return comBloqueio("Salvando…", async () => {
    await api("/api/caixa/config", { method: "PUT", json: { saldo_inicial: saldo, data_saldo: data } });
    return true;
  }).then((ok) => { if (ok) { toast("Saldo inicial atualizado."); carregarCaixa(); } });
}

/* ---------- importar arquivo (JSON/CSV), idempotente ---------- */
async function importarArquivo(arquivo) {
  const sync = $("#cx-sincronizar").checked;
  const enviar = (simular) => {
    const form = new FormData();
    form.append("arquivo", arquivo, arquivo.name);
    return api(`/api/caixa/importar?simular=${simular}&sincronizar=${sync}`, { method: "POST", body: form });
  };
  const prev = await comBloqueio("Conferindo o arquivo…", () => enviar(true));
  if (!prev) return;
  const msg = `Criar ${prev.criados}, atualizar ${prev.atualizados}, manter ${prev.iguais}` + (sync ? `, remover ${prev.removidos}` : "") + ". Continuar?";
  if (!confirm(msg)) return;
  const r = await comBloqueio("Importando…", () => enviar(false));
  if (r) { toast(`Importado: ${r.criados} novos, ${r.atualizados} atualizados, ${r.iguais} iguais.`); carregarCaixa(); }
}

/* ---------- sugestão de lançamento ao salvar uma nota ---------- */
let sugestaoAtual = null;

async function sugerirLancamento(chave) {
  try {
    sugestaoAtual = await api(`/api/caixa/sugestao/${encodeURIComponent(chave)}`);
  } catch { return; } // sem sugestão não atrapalha o fluxo da nota
  $("#sug-texto").textContent = `${sugestaoAtual.nome}: ${brl.format(sugestaoAtual.valor)} em ${diaMes(sugestaoAtual.data)}. Lançar no caixa?`;
  $("#sugestao").hidden = false;
  $("#sug-diario").focus();
}

async function confirmarSugestao(tipo) {
  const s = sugestaoAtual;
  $("#sugestao").hidden = true;
  sugestaoAtual = null;
  if (!s || !tipo) return;
  await comBloqueio("Lançando…", async () => {
    try {
      await api("/api/caixa/lancamentos", { method: "POST", json: { ...s, tipo } });
      toast(`Lançado no caixa como ${TIPOS_CX[tipo].toLowerCase()}.`);
    } catch (e) {
      if (e.status === 409) toast("Essa nota já está no caixa.", true); else throw e;
    }
  });
}

/* ---------- eventos ---------- */
function ligarEventosCaixa() {
  document.querySelectorAll(".segmentos button").forEach((b) => b.addEventListener("click", () => mostrarSub(b.dataset.sub)));
  $("#cx-mes").addEventListener("change", renderSaldos);
  $("#form-config-caixa").addEventListener("submit", salvarConfigCaixa);
  $("#form-lanc").addEventListener("submit", salvarLancamento);
  $("#btn-lanc-cancelar").addEventListener("click", limparFormLanc);
  $("#cx-importar").addEventListener("change", (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (f) importarArquivo(f);
  });
  $("#sug-diario").addEventListener("click", () => confirmarSugestao("diario"));
  $("#sug-cartao").addEventListener("click", () => confirmarSugestao("cartao"));
  $("#sug-pular").addEventListener("click", () => confirmarSugestao(null));
  limparFormLanc();
}
document.addEventListener("DOMContentLoaded", ligarEventosCaixa);

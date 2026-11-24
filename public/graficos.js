"use strict";

/* Gráficos em SVG próprio (sem biblioteca; a CSP só permite 'self'). Cores e fontes vêm das classes .g-* de styles.css.
 * Todo texto entra por createTextNode/setAttribute, nunca por innerHTML. */

const SVGNS = "http://www.w3.org/2000/svg";
const PALETA = ["#00875a", "#3b6fd4", "#d49a2f", "#8a4fd1", "#17a2b8", "#d64545", "#6b8e23", "#c0762f", "#e0559b", "#4d7ea8", "#7a8f3d", "#b0763a"];
const COR_OUTROS = "#9aa7a0";

function svg(tag, attrs = {}, ...filhos) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, v);
  for (const f of filhos.flat()) if (f != null) el.append(f instanceof Node ? f : document.createTextNode(String(f)));
  return el;
}

const corDaFatia = (rotulo, i) => (rotulo === "Outros" ? COR_OUTROS : PALETA[i % PALETA.length]);

function arco(cx, cy, R, r, a0, a1) {
  const p = (a, raio) => `${(cx + raio * Math.sin(a)).toFixed(2)} ${(cy - raio * Math.cos(a)).toFixed(2)}`;
  const g = a1 - a0 > Math.PI ? 1 : 0;
  return `M${p(a0, R)}A${R} ${R} 0 ${g} 1 ${p(a1, R)}L${p(a1, r)}A${r} ${r} 0 ${g} 0 ${p(a0, r)}Z`;
}

/** Rosca: fatias = [{rotulo, valor}]; devolve a lista com a cor usada (para montar a legenda). */
function rosca(alvo, fatias, textoCentro, formatar) {
  const total = fatias.reduce((s, f) => s + f.valor, 0);
  const cores = fatias.map((f, i) => ({ ...f, cor: corDaFatia(f.rotulo, i) }));
  const raiz = svg("svg", { viewBox: "0 0 180 180", class: "g-rosca", role: "img", "aria-label": `Gastos por categoria, total ${formatar(total)}` });
  let a = 0;
  for (const f of cores) {
    const ang = total ? Math.min((f.valor / total) * 2 * Math.PI, 2 * Math.PI - 0.0001) : 0;
    if (ang > 0) raiz.append(svg("path", { d: arco(90, 90, 82, 52, a, a + ang), fill: f.cor }, svg("title", {}, `${f.rotulo}: ${formatar(f.valor)}`)));
    a += ang;
  }
  raiz.append(svg("text", { x: 90, y: 86, "text-anchor": "middle", class: "g-centro-rotulo" }, textoCentro || "Total"));
  raiz.append(svg("text", { x: 90, y: 104, "text-anchor": "middle", class: "g-centro-valor" }, formatar(total)));
  alvo.replaceChildren(raiz);
  return cores;
}

/** Barras acima/abaixo do zero. itens = [{rotulo, valor, destaque?}]. Verde = positivo, vermelho = negativo. */
function barrasZero(alvo, itens, formatar, descricao) {
  const W = 360, H = 190, topo = 20, base = 36, pad = 6;
  const pmax = Math.max(0, ...itens.map((i) => i.valor)), nmax = Math.abs(Math.min(0, ...itens.map((i) => i.valor)));
  const escala = (H - topo - base) / (pmax + nmax || 1);
  const zero = topo + pmax * escala;
  const larg = (W - pad * 2) / itens.length;
  const raiz = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "g-barras", role: "img", "aria-label": descricao });
  raiz.append(svg("line", { x1: pad, x2: W - pad, y1: zero, y2: zero, class: "g-eixo" }));
  itens.forEach((it, i) => {
    const x = pad + i * larg + larg * 0.15, w = larg * 0.7, h = Math.abs(it.valor) * escala;
    const y = it.valor >= 0 ? zero - h : zero;
    raiz.append(svg("rect", { x: x.toFixed(1), y: y.toFixed(1), width: w.toFixed(1), height: Math.max(h, 1).toFixed(1), rx: 3,
      class: `${it.valor < 0 ? "g-neg" : "g-pos"}${it.destaque ? " g-destaque" : ""}` }, svg("title", {}, `${it.rotulo}: ${formatar(it.valor)}`)));
    raiz.append(svg("text", { x: (x + w / 2).toFixed(1), y: H - 6, "text-anchor": "middle", class: "g-txt" }, it.rotulo));
    raiz.append(svg("text", { x: (x + w / 2).toFixed(1), y: (it.valor >= 0 ? y - 3 : y + h + 10).toFixed(1), "text-anchor": "middle", class: "g-txt g-val" },
      Math.abs(it.valor) >= 1000 ? `${(it.valor / 1000).toFixed(1).replace(".", ",")}k` : String(Math.round(it.valor))));
  });
  alvo.replaceChildren(raiz);
}

/** Linha do saldo ao longo do tempo. pontos = [{rotulo, valor}]; marca o menor ponto e a linha do zero. */
function linhaSaldo(alvo, pontos, formatar, descricao, rotulosX = 4) {
  const W = 360, H = 170, esq = 6, dir = 6, topo = 14, base = 22;
  const vals = pontos.map((p) => p.valor);
  const min = Math.min(0, ...vals), max = Math.max(0, ...vals), faixa = max - min || 1;
  const x = (i) => esq + ((W - esq - dir) * i) / Math.max(1, pontos.length - 1);
  const y = (v) => topo + (H - topo - base) * (1 - (v - min) / faixa);
  const raiz = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "g-linha", role: "img", "aria-label": descricao });
  raiz.append(svg("line", { x1: esq, x2: W - dir, y1: y(0), y2: y(0), class: "g-eixo" }));
  raiz.append(svg("polyline", { points: pontos.map((p, i) => `${x(i).toFixed(1)},${y(p.valor).toFixed(1)}`).join(" "), class: "g-trilha" }));
  const imin = vals.indexOf(Math.min(...vals));
  if (pontos.length) {
    raiz.append(svg("circle", { cx: x(imin).toFixed(1), cy: y(vals[imin]).toFixed(1), r: 4, class: vals[imin] < 0 ? "g-pt-neg" : "g-pt" },
      svg("title", {}, `Menor saldo: ${formatar(vals[imin])} em ${pontos[imin].rotulo}`)));
    for (let k = 0; k <= rotulosX; k++) {
      const i = Math.round(((pontos.length - 1) * k) / rotulosX);
      raiz.append(svg("text", { x: x(i).toFixed(1), y: H - 6, "text-anchor": k === 0 ? "start" : k === rotulosX ? "end" : "middle", class: "g-txt" }, pontos[i].rotulo));
    }
  }
  alvo.replaceChildren(raiz);
}

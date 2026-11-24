/* nfce-scanner.js - captura de NFC-e de Goiás no navegador (extraído do SmartMercado). Sem framework.
 *
 * Faz: leitura do QR Code pela câmera, foto do cupom (comprimida para < 1,5 MB) e as chamadas à API
 * criada por nfce_scanner.py. NÃO desenha tela de revisão nem salva nada: você recebe a nota pronta e
 * decide o que fazer (formulário, lançamento financeiro, etc.).
 *
 * Precisa de:  <script src="html5-qrcode.min.js"></script>  (npm: html5-qrcode@2.3.8) só para a câmera.
 *
 * Uso:
 *   const nfce = NfceScanner.criar({
 *     baseUrl: "/api/nfce",                                   // onde o router foi montado
 *     headers: () => ({ Authorization: "Bearer " + token }),  // sua autenticação (opcional)
 *   });
 *   await nfce.iniciarCamera("leitor", {                      // <div id="leitor"></div>
 *     aoLerNota: (nota) => abrirFormulario(nota),
 *     aoErro: (erro) => alert(erro.message),
 *   });
 *   const nota = await nfce.lerFoto(inputFile.files[0]);      // alternativa pela foto
 *   const nota2 = await nfce.lerLink(textoDoQr);              // ou direto por um link
 *   await nfce.parar();
 *
 * Ao montar a tela com dados da nota, use textContent/value (nunca innerHTML): nome de produto vem de fora.
 */
(function (global) {
  "use strict";

  const HOST_NFCE = "nfeweb.sefaz.go.gov.br";
  const LIMITE_FOTO = 1.5 * 1024 * 1024;
  const TOLERANCIA = 0.05;

  const arred = (v) => Math.round(v * 100) / 100;

  /** Só https://nfeweb.sefaz.go.gov.br, sem usuário/senha (o servidor valida de novo). */
  function linkValido(texto) {
    try {
      const u = new URL(String(texto).trim());
      return u.protocol === "https:" && u.hostname === HOST_NFCE && !u.username && !u.password;
    } catch {
      return false;
    }
  }

  /** Recalcula soma_itens / diferenca / soma_confere depois que o usuário edita itens ou o total. */
  function conferirSoma(nota) {
    const soma = arred(nota.itens.reduce((s, i) => s + Number(i.preco_total || 0), 0));
    const dif = arred(soma - Number(nota.descontos || 0) - Number(nota.total || 0));
    return { ...nota, soma_itens: soma, diferenca: dif, soma_confere: Math.abs(dif) <= TOLERANCIA };
  }

  /** Reduz a foto para < 1,5 MB (JPEG), respeitando a orientação do EXIF. */
  async function comprimirFoto(arquivo) {
    const img = await createImageBitmap(arquivo, { imageOrientation: "from-image" });
    let escala = Math.min(1, 2400 / Math.max(img.width, img.height));
    let qualidade = 0.85;
    try {
      for (let i = 0; i < 12; i++) {
        const c = document.createElement("canvas");
        c.width = Math.round(img.width * escala);
        c.height = Math.round(img.height * escala);
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        const blob = await new Promise((ok) => c.toBlob(ok, "image/jpeg", qualidade));
        if (blob && blob.size < LIMITE_FOTO) return blob;
        if (qualidade > 0.55) qualidade -= 0.1;
        else escala *= 0.85;
      }
    } finally {
      img.close();
    }
    throw new Error("Não consegui reduzir a foto para menos de 1,5 MB.");
  }

  function mensagemDeErro(corpo, status) {
    const d = corpo && corpo.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((e) => `${(e.loc || []).slice(1).join(" › ")}: ${e.msg}`).join("\n");
    return `Erro ${status}.`;
  }

  function criar({ baseUrl = "/api/nfce", headers = () => ({}) } = {}) {
    let leitor = null;

    async function chamar(caminho, init = {}) {
      let resp;
      try {
        resp = await fetch(baseUrl + caminho, { ...init, headers: { ...headers(), ...(init.headers || {}) } });
      } catch {
        throw new Error("Sem conexão com o servidor.");
      }
      const dados = await resp.json().catch(() => null);
      if (!resp.ok) {
        const e = new Error(mensagemDeErro(dados, resp.status));
        e.status = resp.status;
        throw e;
      }
      return dados;
    }

    function lerLink(url) {
      if (!linkValido(url)) {
        return Promise.reject(new Error("Esse QR Code não é de uma NFC-e de Goiás (nfeweb.sefaz.go.gov.br, HTTPS)."));
      }
      return chamar("/link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: String(url).trim() }),
      });
    }

    async function lerFoto(arquivo) {
      const form = new FormData();
      form.append("foto", await comprimirFoto(arquivo), "nota.jpg");
      return chamar("/foto", { method: "POST", body: form });
    }

    async function parar() {
      const s = leitor;
      leitor = null;
      if (s) {
        try {
          if (s.isScanning) await s.stop();
          s.clear();
        } catch {
          /* já parado */
        }
      }
    }

    /** Abre a câmera traseira no elemento `idElemento`; ao ler um QR válido fecha a câmera e consulta a SEFAZ. */
    async function iniciarCamera(idElemento, { aoLerNota, aoErro = () => {} } = {}) {
      if (typeof global.Html5Qrcode === "undefined") {
        return aoErro(new Error("Leitor de QR Code não carregou (html5-qrcode)."));
      }
      await parar();
      leitor = new global.Html5Qrcode(idElemento, { verbose: false });
      try {
        await leitor.start(
          { facingMode: "environment" },
          { fps: 10, qrbox: (w, h) => { const l = Math.floor(Math.min(w, h) * 0.75); return { width: l, height: l }; } },
          async (texto) => {
            await parar();
            try {
              aoLerNota(await lerLink(texto));
            } catch (e) {
              aoErro(e);
            }
          },
          () => {}
        );
      } catch {
        await parar();
        aoErro(new Error("Não foi possível abrir a câmera. Verifique a permissão."));
      }
    }

    return { linkValido, lerLink, lerFoto, iniciarCamera, parar };
  }

  const api = { criar, linkValido, comprimirFoto, conferirSoma };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.NfceScanner = api;
})(typeof window !== "undefined" ? window : globalThis);

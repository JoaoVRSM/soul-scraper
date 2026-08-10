const examples = {
  javascript: `const response = await fetch("http://127.0.0.1:8787/api/v1/scrape", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    url: "https://seu-site.com",
    output: "catalog",
    render_js: true,
    validate_links: true
  })
});

if (!response.ok) throw new Error("Falha na raspagem");
const catalogo = await response.json();
console.log(catalogo.filmes, catalogo.series);`,
  php: `<?php
$payload = json_encode([
    "url" => "https://seu-site.com",
    "output" => "catalog",
    "render_js" => true,
    "validate_links" => true
]);

$ch = curl_init("http://127.0.0.1:8787/api/v1/scrape");
curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_HTTPHEADER => ["Content-Type: application/json"],
    CURLOPT_POSTFIELDS => $payload,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => 0
]);

$catalogo = json_decode(curl_exec($ch), true);
curl_close($ch);`,
  python: `import requests

response = requests.post(
    "http://127.0.0.1:8787/api/v1/scrape",
    json={
        "url": "https://seu-site.com",
        "output": "catalog",
        "render_js": True,
        "validate_links": True,
    },
    timeout=None,
)
response.raise_for_status()
catalogo = response.json()`,
  curl: `curl -X POST "http://127.0.0.1:8787/api/v1/scrape" \\
  -H "Content-Type: application/json" \\
  -d '{
    "url": "https://seu-site.com",
    "output": "catalog",
    "render_js": true,
    "validate_links": true
  }'`
};

const codeExample = document.querySelector("#code-example");
const copyButton = document.querySelector("#copy-code");
let currentLanguage = "javascript";
let resultData = null;
let resultFilename = "catalogo-links.json";

function showExample(language) {
  currentLanguage = language;
  codeExample.textContent = examples[language];
  document.querySelectorAll(".code-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.language === language);
  });
}

document.querySelectorAll(".code-tab").forEach((tab) => {
  tab.addEventListener("click", () => showExample(tab.dataset.language));
});

copyButton.addEventListener("click", async () => {
  await navigator.clipboard.writeText(examples[currentLanguage]);
  copyButton.textContent = "Copiado!";
  setTimeout(() => { copyButton.textContent = "Copiar código"; }, 1400);
});

const form = document.querySelector("#try-form");
const tryButton = document.querySelector("#try-button");
const resultStatus = document.querySelector("#result-status");
const resultEmpty = document.querySelector("#result-empty");
const resultJson = document.querySelector("#result-json");
const resultCode = resultJson.querySelector("code");
const errorBox = document.querySelector("#try-error");
const downloadButton = document.querySelector("#download-result");

const outputFiles = {
  catalog: "catalogo-links.json",
  movies: "filmes-links.json",
  series: "series-links.json",
  full: "resultado.json",
};

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  downloadButton.hidden = true;
  resultEmpty.hidden = true;
  resultJson.hidden = false;
  resultCode.textContent = "Raspagem em andamento...\n\nA API responderá assim que o JSON estiver pronto.";
  resultStatus.textContent = "Processando o site...";
  tryButton.disabled = true;
  tryButton.textContent = "Aguardando a raspagem";

  const output = document.querySelector("#try-output").value;
  try {
    const response = await fetch("/api/v1/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: document.querySelector("#try-url").value.trim(),
        output,
        max_pages: Number(document.querySelector("#try-pages").value),
        max_depth: 10,
        concurrency: 12,
        browser_concurrency: 2,
        render_js: document.querySelector("#try-js").checked,
        validate_links: true,
      }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === "string" ? body.detail : `Erro HTTP ${response.status}`);
    }
    resultData = await response.json();
    resultFilename = outputFiles[output];
    const formatted = JSON.stringify(resultData, null, 2);
    resultCode.textContent = formatted.length > 30000
      ? `${formatted.slice(0, 30000)}\n\n... prévia limitada. Baixe o JSON para ver tudo.`
      : formatted;
    resultStatus.textContent = `Concluído · ${resultFilename}`;
    downloadButton.hidden = false;
  } catch (error) {
    resultJson.hidden = true;
    resultEmpty.hidden = false;
    resultStatus.textContent = "A requisição falhou";
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    tryButton.disabled = false;
    tryButton.textContent = "Executar e receber JSON";
  }
});

downloadButton.addEventListener("click", () => {
  if (resultData === null) return;
  const blob = new Blob([JSON.stringify(resultData, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = resultFilename;
  link.click();
  URL.revokeObjectURL(url);
});

fetch("/api/v1/health")
  .then((response) => {
    if (!response.ok) throw new Error();
  })
  .catch(() => {
    document.querySelector("#api-state").textContent = "API INDISPONÍVEL";
    document.querySelector(".online i").style.background = "#ff777c";
  });

showExample("javascript");

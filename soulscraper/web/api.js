const examples = {
  javascript: `// No backend do seu site (Node.js)
const response = await fetch("https://sua-api.com/api/v1/scrape", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    url: "seu-site.com"
  })
});

if (!response.ok) throw new Error("Falha na raspagem");
const catalogo = await response.json();
console.log(catalogo.filmes, catalogo.series);`,
  php: `<?php
$payload = json_encode(["url" => "seu-site.com"]);

$ch = curl_init("https://sua-api.com/api/v1/scrape");
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
    "https://sua-api.com/api/v1/scrape",
    json={"url": "seu-site.com"},
    timeout=None,
)
response.raise_for_status()
catalogo = response.json()`,
  curl: `curl -X POST "https://sua-api.com/api/v1/scrape" \\
  -H "Content-Type: application/json" \\
  -d '{"url":"seu-site.com"}'`
};

const codeExample = document.querySelector("#code-example");
const copyButton = document.querySelector("#copy-code");
let currentLanguage = "javascript";

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

fetch("/api/v1/health")
  .then((response) => {
    if (!response.ok) throw new Error();
  })
  .catch(() => {
    document.querySelector("#api-state").textContent = "API INDISPONÍVEL";
    document.querySelector(".online i").style.background = "#ff777c";
  });

showExample("javascript");

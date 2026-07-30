const $ = (selector) => document.querySelector(selector);

const state = {
  jobId: null,
  timer: null,
  latestJob: null,
  searchTimer: null,
};

const form = $("#crawl-form");
const startButton = $("#start-button");
const cancelButton = $("#cancel-button");
const formError = $("#form-error");
const emptyMonitor = $("#empty-monitor");
const activeMonitor = $("#active-monitor");
const resultsSection = $("#results-section");
const activitySection = $("#activity-section");
const performanceProfiles = {
  balanced: { concurrency: 12, browser: 2 },
  powerful: { concurrency: 24, browser: 6 },
  maximum: { concurrency: 40, browser: 10 },
};

const statusLabels = {
  queued: "Na fila",
  running: "Varrendo",
  validating: "Validando links",
  reextracting: "Reextraindo",
  completed: "Concluída",
  failed: "Falhou",
  cancelled: "Cancelada",
};

function numberValue(selector) {
  return Number($(selector).value);
}

function payloadFromForm() {
  return {
    url: $("#url").value.trim(),
    max_pages: numberValue("#max-pages"),
    max_depth: numberValue("#max-depth"),
    concurrency: numberValue("#concurrency"),
    browser_concurrency: numberValue("#browser-concurrency"),
    timeout: numberValue("#timeout"),
    delay: numberValue("#delay"),
    max_mb: numberValue("#max-mb"),
    subdomains: $("#subdomains").checked,
    render_js: $("#render-js").checked,
    validate_links: $("#validate-links").checked,
    sitemap: $("#sitemap").checked,
    respect_robots: $("#respect-robots").checked,
  };
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `Erro HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((item) => item.msg).join(" · ");
    } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function setError(message = "") {
  formError.textContent = message;
  formError.hidden = !message;
}

function setJobStatus(status) {
  const badge = $("#job-state");
  badge.textContent = statusLabels[status] || "Aguardando";
  badge.className = `status-badge ${status || "idle"}`;
  $("#system-label").textContent = status === "running" ? "Auditoria em curso" : "Sistema pronto";
}

function updateJob(job) {
  state.latestJob = job;
  emptyMonitor.hidden = true;
  activeMonitor.hidden = false;
  activitySection.hidden = false;
  setJobStatus(job.status);
  $("#target-url").textContent = job.target_url || $("#url").value.trim() || "—";
  $("#processed-count").textContent = job.processed.toLocaleString("pt-BR");
  $("#discovered-count").textContent = job.discovered.toLocaleString("pt-BR");
  $("#findings-count").textContent = job.findings_count.toLocaleString("pt-BR");
  ["byse", "doodstream", "mixdrop", "streamtape"].forEach((provider) => {
    $(`#${provider}-count`).textContent = (job.providers[provider] || 0).toLocaleString("pt-BR");
  });

  const health = job.health || {};
  const healthy = (health.working || 0) + (health.working_browser || 0) + (health.redirected || 0);
  const attention = (health.browser_required || 0) + (health.timeout || 0) +
    (health.rate_limited || 0) + (health.unknown || 0);
  const broken = (health.dead || 0) + (health.malformed || 0) + (health.blocked_private || 0);
  $("#healthy-count").textContent = healthy.toLocaleString("pt-BR");
  $("#attention-count").textContent = attention.toLocaleString("pt-BR");
  $("#broken-count").textContent = broken.toLocaleString("pt-BR");

  const estimated = Math.max(job.discovered, 1);
  const isValidating = job.status === "validating";
  const progressBase = isValidating
    ? job.validated_count / Math.max(job.validation_total, 1)
    : job.processed / estimated;
  const progress = job.status === "completed"
    ? 100
    : Math.max(4, Math.min(96, progressBase * 100));
  $("#progress-bar").style.width = `${progress}%`;
  const activeStatuses = ["queued", "running", "validating", "reextracting"];
  cancelButton.hidden = !activeStatuses.includes(job.status);
  startButton.disabled = activeStatuses.includes(job.status);
  const validationProgress = $("#validation-progress");
  validationProgress.hidden = !isValidating;
  validationProgress.textContent = isValidating
    ? `Validando ${job.validated_count || 0} de ${job.validation_total || 0} links`
    : "";

  renderActivity(job.latest_pages || []);

  if (job.status === "completed") {
    clearInterval(state.timer);
    state.timer = null;
    startButton.disabled = false;
    resultsSection.hidden = false;
    configureDownloads(job.downloads);
    loadFindings();
    resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
  } else if (job.status === "failed" || job.status === "cancelled") {
    clearInterval(state.timer);
    state.timer = null;
    startButton.disabled = false;
    if (job.error) setError(job.error);
  }
}

function renderActivity(pages) {
  const list = $("#activity-list");
  list.replaceChildren();
  [...pages].reverse().slice(0, 12).forEach((page) => {
    const row = document.createElement("div");
    row.className = "activity-item";

    const status = document.createElement("span");
    status.className = `http-status ${page.status && page.status < 400 ? "" : "error"}`;
    status.textContent = page.status || "ERR";

    const url = document.createElement("span");
    url.className = "activity-url";
    url.textContent = page.url;
    url.title = page.url;

    const time = document.createElement("span");
    time.className = "activity-meta";
    time.textContent = `${page.elapsed_ms || 0} ms`;

    const refs = document.createElement("span");
    refs.className = "activity-meta";
    refs.textContent = `${page.findings_found || 0} refs`;

    row.append(status, url, time, refs);
    list.append(row);
  });
}

function configureDownloads(downloads) {
  $("#download-catalog").href = downloads["catalogo-links.json"];
  $("#download-movies").href = downloads["filmes-links.json"];
  $("#download-series").href = downloads["series-links.json"];
  $("#download-json").href = downloads["resultado.json"];
  $("#download-findings").href = downloads["referencias.csv"];
  $("#download-pages").href = downloads["paginas.csv"];
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const job = await api(`/api/jobs/${state.jobId}`);
    updateJob(job);
  } catch (error) {
    clearInterval(state.timer);
    state.timer = null;
    startButton.disabled = false;
    setError(error.message);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError();
  resultsSection.hidden = true;
  startButton.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify(payloadFromForm()),
    });
    state.jobId = job.id;
    localStorage.setItem("soulCrawlerJobId", job.id);
    updateJob(job);
    clearInterval(state.timer);
    state.timer = setInterval(pollJob, 850);
    await pollJob();
  } catch (error) {
    startButton.disabled = false;
    setError(error.message);
  }
});

cancelButton.addEventListener("click", async () => {
  if (!state.jobId) return;
  cancelButton.disabled = true;
  try {
    const job = await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
    updateJob(job);
  } catch (error) {
    setError(error.message);
  } finally {
    cancelButton.disabled = false;
  }
});

async function loadFindings() {
  if (!state.jobId) return;
  const provider = $("#provider-filter").value;
  const health = $("#health-filter").value;
  const search = $("#results-search").value.trim();
  const params = new URLSearchParams({ provider, health, limit: "500" });
  if (search) params.set("search", search);
  try {
    const response = await api(`/api/jobs/${state.jobId}/findings?${params}`);
    renderFindings(response.items);
  } catch (error) {
    setError(error.message);
  }
}

function renderFindings(items) {
  const body = $("#results-body");
  const empty = $("#results-empty");
  body.replaceChildren();
  empty.hidden = items.length > 0;
  items.forEach((item) => {
    const row = document.createElement("tr");
    const movie = document.createElement("td");
    movie.className = "movie-cell";
    movie.textContent = item.movie_name || "Não identificado";

    const tmdb = document.createElement("td");
    tmdb.className = "tmdb-cell";
    if (item.tmdb_id) {
      const tmdbLink = document.createElement("a");
      tmdbLink.href = `https://www.themoviedb.org/movie/${item.tmdb_id}`;
      tmdbLink.target = "_blank";
      tmdbLink.rel = "noopener noreferrer";
      tmdbLink.textContent = `#${item.tmdb_id}`;
      tmdb.append(tmdbLink);
    } else {
      tmdb.textContent = "—";
    }

    const provider = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = "provider-chip";
    chip.textContent = item.provider;
    provider.append(chip);

    const healthCell = document.createElement("td");
    const healthChip = document.createElement("span");
    healthChip.className = `health-chip ${item.health_status || "unchecked"}`;
    healthChip.textContent = healthLabel(item.health_status, item.health_http_status);
    healthCell.append(healthChip);

    const source = document.createElement("td");
    source.className = "url-cell";
    source.textContent = item.source_page;

    const reference = document.createElement("td");
    reference.className = "reference-cell";
    reference.textContent = item.resolved_url || item.reference;

    const context = document.createElement("td");
    context.className = "context-cell";
    context.textContent = item.context;
    row.append(movie, tmdb, provider, healthCell, source, reference, context);
    body.append(row);
  });
}

function healthLabel(status, httpStatus) {
  const labels = {
    working: "Funcionando",
    working_browser: "Navegador OK",
    redirected: "Redirecionado",
    browser_required: "Exige navegador",
    rate_limited: "Limite de acesso",
    dead: "Quebrado",
    timeout: "Timeout",
    malformed: "Malformado",
    blocked_private: "Host bloqueado",
    unknown: "Indefinido",
    unchecked: "Não verificado",
  };
  const label = labels[status] || status || "Não verificado";
  return httpStatus ? `${label} · ${httpStatus}` : label;
}

$("#provider-filter").addEventListener("change", loadFindings);
$("#health-filter").addEventListener("change", loadFindings);
$("#performance-profile").addEventListener("change", (event) => {
  const profile = performanceProfiles[event.target.value];
  if (!profile) return;
  $("#concurrency").value = profile.concurrency;
  $("#browser-concurrency").value = profile.browser;
});
$("#results-search").addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(loadFindings, 260);
});

async function runJobAction(endpoint) {
  if (!state.jobId) return;
  setError();
  try {
    const job = await api(`/api/jobs/${state.jobId}/${endpoint}`, { method: "POST" });
    updateJob(job);
    clearInterval(state.timer);
    state.timer = setInterval(pollJob, 850);
    await pollJob();
  } catch (error) {
    setError(error.message);
  }
}

$("#revalidate-button").addEventListener("click", () => runJobAction("validate"));
$("#reextract-button").addEventListener("click", () => runJobAction("reextract"));

async function resumeLastJob() {
  const savedJobId = localStorage.getItem("soulCrawlerJobId");
  if (!savedJobId) return;
  try {
    state.jobId = savedJobId;
    const job = await api(`/api/jobs/${savedJobId}`);
    $("#url").value = job.target_url || "";
    updateJob(job);
    if (["queued", "running", "validating", "reextracting"].includes(job.status)) {
      state.timer = setInterval(pollJob, 850);
    }
  } catch (_) {
    localStorage.removeItem("soulCrawlerJobId");
    state.jobId = null;
  }
}

api("/api/health").catch(() => {
  $("#system-label").textContent = "Servidor indisponível";
  $(".pulse").style.background = "#ff6d72";
});
resumeLastJob();

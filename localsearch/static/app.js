// app.js — интерактивная панель краулера: запуск, статус, прогресс в реальном времени.
(function () {
  "use strict";
  const fill = document.getElementById("progress-fill");
  const text = document.getElementById("progress-text");
  const btnCrawl = document.getElementById("btn-crawl");
  const btnStop = document.getElementById("btn-stop");
  const btnSave = document.getElementById("btn-save");
  const targetInput = document.getElementById("crawl-target");
  const depthInput = document.getElementById("crawl-depth");
  if (!fill || !text || !btnCrawl) return; // страница предпросмотра — скрипт не нужен

  let pollTimer = null;

  function setButtons(running) {
    btnCrawl.disabled = running;
    btnStop.disabled = !running;
    targetInput.disabled = running;
  }

  function render(p) {
    const pct = p.percent >= 0 ? p.percent : null;
    fill.style.width = (pct === null ? 5 : pct) + "%";
    fill.classList.toggle("indeterminate", pct === null && p.status === "running");
    const statusMap = {
      idle: "ожидание", running: "сканирование", done: "готово",
      stopped: "остановлен", error: "ошибка"
    };
    let line = `Краулер: ${statusMap[p.status] || p.status}`;
    if (pct !== null) line += ` · ${pct}%`;
    line += ` · обработано ${p.processed}/${p.total_estimated || "?"}` +
            ` · в индексе +${p.indexed} · пропущено ${p.skipped} · ошибок ${p.errors}`;
    if (p.current) line += ` · ${p.current}`;
    text.textContent = line;
    setButtons(p.status === "running");
    if (p.status === "done" || p.status === "stopped" || p.status === "error") {
      stopPolling();
      if (p.status === "done") setTimeout(() => location.reload(), 600);
    }
  }

  async function poll() {
    try {
      const resp = await fetch("/api/status");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      render(data.progress);
    } catch (e) {
      text.textContent = "Нет связи с сервером: " + e.message;
    }
  }

  function startPolling() {
    stopPolling();
    poll();
    pollTimer = setInterval(poll, 800);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  btnCrawl.addEventListener("click", async () => {
    const target = targetInput.value.trim();
    if (!target) { text.textContent = "Укажите путь к папке или URL"; return; }
    btnCrawl.disabled = true;
    try {
      const resp = await fetch("/api/crawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: target, depth: parseInt(depthInput.value || "3", 10) })
      });
      const data = await resp.json();
      if (!resp.ok) { text.textContent = data.error || "Не удалось запустить сканирование"; btnCrawl.disabled = false; return; }
      startPolling();
    } catch (e) {
      text.textContent = "Ошибка запуска: " + e.message;
      btnCrawl.disabled = false;
    }
  });

  btnStop.addEventListener("click", async () => {
    btnStop.disabled = true;
    try { await fetch("/api/crawl/stop", { method: "POST" }); } catch (e) { /* опрос обновит статус */ }
  });

  btnSave.addEventListener("click", async () => {
    try {
      const resp = await fetch("/api/save", { method: "POST" });
      const data = await resp.json();
      text.textContent = resp.ok ? `Индекс сохранён: ${data.path}` : (data.error || "Ошибка сохранения");
    } catch (e) {
      text.textContent = "Ошибка сохранения: " + e.message;
    }
  });

  // При загрузке страницы, если краулер уже работает — подключаемся к прогрессу.
  fetch("/api/status").then(r => r.json()).then(d => {
    if (d.progress && d.progress.status === "running") startPolling();
    else render(d.progress || { status: "idle", processed: 0, indexed: 0, skipped: 0, errors: 0, percent: -1 });
  }).catch(() => {});
})();

/* ата-та sketchup — фронт без фреймворков, потому что тут нечего разводить. */

const $ = (id) => document.getElementById(id);

const el = {
  drop: $("drop"), file: $("file"), pick: $("pick"),
  progress: $("progress"), stage: $("stage"), bar: $("bar"), elapsed: $("elapsed"),
  error: $("error"), errorText: $("error-text"),
  report: $("report"), summary: $("summary"), composition: $("composition"),
  findings: $("findings"), picked: $("picked"), saving: $("saving"),
  whip: $("whip"), result: $("result"), resultBody: $("result-body"),
};

let analysis = null;
let analysisJobId = null;

// ---------------------------------------------------------------- утилиты

function bytes(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} Б`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} КБ`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} МБ`;
  return `${(n / 1024 ** 3).toFixed(2)} ГБ`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** Минимальная разметка: `код` и пустая строка как разделитель абзацев. */
function rich(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .split(/\n\n+/)
    .map((p) => `<p class="f-summary">${p.replace(/\n/g, "<br>")}</p>`)
    .join("");
}

function show(section) { section.classList.remove("hidden"); }
function hide(section) { section.classList.add("hidden"); }

function fail(message) {
  hide(el.progress);
  el.errorText.textContent = message;
  show(el.error);
}

// ---------------------------------------------------------------- загрузка

el.pick.addEventListener("click", (e) => { e.stopPropagation(); el.file.click(); });
el.drop.addEventListener("click", () => el.file.click());
el.file.addEventListener("change", () => { if (el.file.files[0]) upload(el.file.files[0]); });

["dragenter", "dragover"].forEach((ev) =>
  el.drop.addEventListener(ev, (e) => { e.preventDefault(); el.drop.classList.add("over"); })
);
["dragleave", "drop"].forEach((ev) =>
  el.drop.addEventListener(ev, (e) => { e.preventDefault(); el.drop.classList.remove("over"); })
);
el.drop.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) upload(f);
});

function upload(file) {
  if (!file.name.toLowerCase().endsWith(".skp")) {
    fail("нужен файл .skp, а это что-то другое");
    show(el.error);
    return;
  }
  hide(el.drop); hide(el.error); hide(el.report); hide(el.result);
  show(el.progress);
  el.stage.textContent = "заливаю файл";
  el.bar.style.width = "0%";

  const form = new FormData();
  form.append("file", file);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.addEventListener("progress", (e) => {
    if (!e.lengthComputable) return;
    const pct = (e.loaded / e.total) * 100;
    el.bar.style.width = `${pct.toFixed(1)}%`;
    el.elapsed.textContent = `${bytes(e.loaded)} из ${bytes(e.total)}`;
  });
  xhr.addEventListener("load", () => {
    let data;
    try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
    if (xhr.status >= 400) {
      fail(data.detail || `сервер ответил ${xhr.status}`);
      return;
    }
    analysisJobId = data.job_id;
    el.stage.textContent = "файл на месте, начинаю разбор";
    el.bar.style.width = "0%";
    poll(data.job_id, onAnalysisDone);
  });
  xhr.addEventListener("error", () => fail("сеть отвалилась при загрузке"));
  xhr.send(form);
}

// ---------------------------------------------------------------- опрос

function poll(jobId, done) {
  const tick = async () => {
    let job;
    try {
      const res = await fetch(`/api/job/${jobId}`);
      job = await res.json();
      if (!res.ok) { fail(job.detail || "задача потерялась"); return; }
    } catch (e) {
      fail(`не достучался до сервера: ${e}`);
      return;
    }

    el.stage.textContent = job.stage;
    el.bar.style.width = `${(job.progress * 100).toFixed(1)}%`;
    el.elapsed.textContent = `прошло ${job.elapsed} с`;

    if (job.status === "done") { done(job); return; }
    if (job.status === "error") { fail(job.error || "задача упала"); return; }
    setTimeout(tick, 700);
  };
  tick();
}

// ---------------------------------------------------------------- отчёт

function onAnalysisDone(job) {
  analysis = job.result;
  hide(el.progress);
  renderSummary(analysis);
  renderComposition(analysis);
  renderFindings(analysis);
  show(el.report);
  updatePicked();
  el.report.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderSummary(data) {
  const f = data.file;
  const sev = data.summary.by_severity || {};
  const crit = (sev.critical || 0) + (sev.high || 0);

  let verdict;
  if (crit >= 4) verdict = "модель заслужила по полной ⚡ серьёзных провинностей выше крыши";
  else if (crit >= 1) verdict = "есть за что выпороть, но не всё потеряно";
  else if (data.summary.total > 0) verdict = "по мелочи есть замечания, в целом терпимо ♡";
  else verdict = "чистенько! кто-то хорошо себя вёл ✿";

  el.summary.innerHTML = `
    <div class="sum-head">
      <span class="sum-file">📄 ${esc(f.name)}</span>
      <span class="badge">SketchUp ${esc(f.version || "?")}</span>
      <span class="badge">${esc(f.units || "?")}</span>
    </div>
    <div class="stats">
      <div class="stat"><b>${bytes(f.size)}</b><span>размер файла</span></div>
      <div class="stat"><b>${bytes(f.model_dat_size)}</b><span>геометрия</span></div>
      <div class="stat"><b>${f.materials}</b><span>материалов</span></div>
      <div class="stat"><b>${f.textures}</b><span>текстур</span></div>
      <div class="stat"><b>${bytes(f.texture_bytes)}</b><span>вес текстур</span></div>
      <div class="stat"><b>${data.summary.total}</b><span>находок</span></div>
    </div>
    <p class="verdict">${esc(verdict)}</p>`;
}

function renderComposition(data) {
  const max = Math.max(...data.composition.map((c) => c.bytes), 1);
  el.composition.innerHTML = data.composition
    .map((c) => `
      <div class="comp-row">
        <span>${esc(c.group)} <span class="hint">(${c.count})</span></span>
        <div class="comp-bar"><div class="comp-fill" style="width:${(c.bytes / max) * 100}%"></div></div>
        <span class="comp-val">${bytes(c.bytes)}</span>
      </div>`)
    .join("");
}

const KIND_LABEL = { auto: "автомат", sdk: "нужен SDK", manual: "руками" };

function renderFindings(data) {
  if (!data.findings.length) {
    el.findings.innerHTML = `<p class="hint">ни одной проблемы не нашёл. подозрительно.</p>`;
    return;
  }

  el.findings.innerHTML = data.findings.map((f) => {
    const auto = f.fix_kind === "auto" && f.fix;
    const items = f.items.length
      ? `<details class="f-items">
           <summary>показать список (${f.items_total})</summary>
           <ul class="f-list">${f.items.map((i) => `<li>${esc(i)}</li>`).join("")}
           ${f.items_total > f.items.length
             ? `<li>… ещё ${f.items_total - f.items.length}</li>` : ""}</ul>
         </details>`
      : "";

    return `
      <article class="finding sev-${esc(f.severity)}">
        <div class="f-head">
          <input class="f-check" type="checkbox" ${auto ? "" : "disabled"}
                 data-fix="${esc(f.fix || "")}" data-bytes="${f.bytes_impact}"
                 title="${auto ? "чинится автоматически" : "пока не чинится автоматически"}">
          <div class="f-title">
            <h3>${esc(f.title)}</h3>
            <div class="f-tags">
              <span class="tag tag-${esc(f.severity)}">${esc(f.severity)}</span>
              <span class="tag tag-cat">${esc(f.category)}</span>
              <span class="tag tag-${esc(f.fix_kind)}">${KIND_LABEL[f.fix_kind] || f.fix_kind}</span>
              ${f.bytes_impact ? `<span class="tag tag-save">≈ ${bytes(f.bytes_impact)}</span>` : ""}
            </div>
            ${rich(f.summary)}
            ${f.fix_note ? `<p class="f-note">${esc(f.fix_note)}</p>` : ""}
            ${items}
          </div>
        </div>
      </article>`;
  }).join("");

  el.findings.querySelectorAll(".f-check").forEach((c) =>
    c.addEventListener("change", updatePicked)
  );
}

function selectedFixes() {
  return [...el.findings.querySelectorAll(".f-check:checked")]
    .map((c) => c.dataset.fix)
    .filter(Boolean);
}

function updatePicked() {
  const checked = [...el.findings.querySelectorAll(".f-check:checked")];
  const total = checked.reduce((sum, c) => sum + Number(c.dataset.bytes || 0), 0);
  el.picked.textContent = checked.length;
  el.saving.textContent = total
    ? `освободится примерно: ${bytes(total)}`
    : "освободится примерно: —";
  el.whip.disabled = checked.length === 0;
}

// ---------------------------------------------------------------- порка

el.whip.addEventListener("click", async () => {
  const fixes = [...new Set(selectedFixes())];
  if (!fixes.length) return;

  el.whip.disabled = true;
  hide(el.result);
  show(el.progress);
  el.stage.textContent = "замахиваюсь";
  el.bar.style.width = "0%";
  el.progress.scrollIntoView({ behavior: "smooth", block: "center" });

  let data;
  try {
    const res = await fetch(`/api/job/${analysisJobId}/fix`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fixes }),
    });
    data = await res.json();
    if (!res.ok) { fail(data.detail || `сервер ответил ${res.status}`); return; }
  } catch (e) {
    fail(`не отправилось: ${e}`);
    return;
  }

  poll(data.job_id, (job) => onFixDone(job, data.job_id));
});

function onFixDone(job, fixJobId) {
  hide(el.progress);
  const r = job.result;
  const savedPct = r.size_before ? (r.saved / r.size_before) * 100 : 0;

  el.resultBody.innerHTML = `
    <div class="res-grid">
      <div class="stat"><b>${bytes(r.size_before)}</b><span>было</span></div>
      <div class="stat"><b>${bytes(r.size_after)}</b><span>стало</span></div>
      <div class="stat"><b>${bytes(r.saved)}</b><span>снято (${savedPct.toFixed(1)}%)</span></div>
      <div class="stat"><b>${r.touched_total}</b><span>текстур обработано</span></div>
    </div>
    <p class="${r.verified ? "verify-ok" : "verify-bad"}">
      ${r.verified ? "✔" : "✘"} проверка контейнера: ${esc(r.verify_message)}
    </p>
    ${r.errors.length
      ? `<details class="f-items"><summary>ошибки на отдельных файлах (${r.errors.length})</summary>
         <ul class="f-list">${r.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>`
      : ""}
    <a class="btn dl" href="/api/job/${fixJobId}/download">скачать выпоротый файл ⬇</a>
    <p class="warn">
      Проверьте результат в SketchUp перед тем, как пускать файл в работу.
      Оригинал не изменялся.
    </p>`;
  show(el.result);
  el.whip.disabled = false;
  el.result.scrollIntoView({ behavior: "smooth", block: "start" });
}

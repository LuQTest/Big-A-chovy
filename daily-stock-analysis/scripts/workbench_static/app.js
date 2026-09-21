/* A股 Web 工作台前端逻辑（无外部依赖） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function fetchJSON(url, opts) {
  const resp = await fetch(url, opts);
  return resp.json();
}

async function fetchText(url) {
  const resp = await fetch(url);
  return resp.text();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---- 极简 Markdown 渲染：标题/表格/引用/列表/分隔线/行内加粗代码 ---- */
function inline(s) {
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function renderMD(md) {
  const lines = md.split(/\r?\n/);
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("|") && i + 1 < lines.length && /^\|[\s:|-]+\|?$/.test(lines[i + 1].trim())) {
      // GFM 表格
      const header = line.split("|").slice(1, -1).map((c) => c.trim());
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        rows.push(lines[i].split("|").slice(1, -1).map((c) => c.trim()));
        i++;
      }
      let html = "<table><thead><tr>";
      for (const h of header) html += `<th>${inline(h)}</th>`;
      html += "</tr></thead><tbody>";
      for (const r of rows) {
        html += "<tr>";
        for (let c = 0; c < header.length; c++) {
          let cell = r[c] ?? "";
          // A股红涨绿跌：+x% 红、-x% 绿
          if (/^\+?\d/.test(cell) && cell.includes("%")) {
            const cls = cell.trim().startsWith("-") ? "down" : "up";
            cell = `<span class="${cls}">${inline(cell)}</span>`;
          } else {
            cell = inline(cell);
          }
          html += `<td>${cell}</td>`;
        }
        html += "</tr>";
      }
      html += "</tbody></table>";
      out.push(html);
      continue;
    }
    if (/^###\s/.test(line)) { out.push(`<h4>${inline(line.slice(4))}</h4>`); }
    else if (/^##\s/.test(line)) { out.push(`<h3>${inline(line.slice(3))}</h3>`); }
    else if (/^#\s/.test(line)) { out.push(`<h2>${inline(line.slice(2))}</h2>`); }
    else if (/^(-{3,}|\*{3,})$/.test(line.trim())) { out.push("<hr>"); }
    else if (/^>\s?/.test(line)) { out.push(`<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`); }
    else if (/^[-*]\s/.test(line)) { out.push(`<li>${inline(line.slice(2))}</li>`); }
    else if (line.trim()) { out.push(`<p>${inline(line)}</p>`); }
    i++;
  }
  return out.join("\n");
}

function renderJSON(obj) {
  return `<pre>${esc(JSON.stringify(obj, null, 2))}</pre>`;
}

/* ---- 通用工具调用 ---- */
async function callTool(btn, outSel, fn) {
  const btnText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "查询中...";
  const out = $(outSel);
  out.innerHTML = `<pre>查询中...</pre>`;
  try {
    const data = await fn();
    if (data && data.error) {
      out.innerHTML = `<pre class="err">${esc(JSON.stringify(data, null, 2))}</pre>`;
    } else {
      out.innerHTML = data && data._html ? data._html : renderJSON(data);
    }
  } catch (e) {
    out.innerHTML = `<pre class="err">请求失败: ${esc(e.message || e)}</pre>`;
  } finally {
    btn.disabled = false;
    btn.textContent = btnText;
  }
}

/* ================= Tab 切换 ================= */
$$(".tab-btn[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "reports") loadReports();
  });
});

/* ================= 筛选工作台 ================= */
let pollTimer = null;

$("#run-btn").addEventListener("click", async () => {
  const modes = [];
  if ($("#mode-strict").checked) modes.push("strict");
  if ($("#mode-low").checked) modes.push("low");
  if ($("#mode-watchlist").checked) modes.push("watchlist");
  if (!modes.length) { alert("请至少选择一个筛选模块"); return; }
  const body = {
    modes,
    top: parseInt($("#top").value, 10) || 15,
    network_mode: $("#network-mode").value,
    skip_announcements: $("#skip-announcements").checked,
    skip_capital_ranking: $("#skip-capital").checked,
  };
  try {
    const res = await fetchJSON("/api/wb/screen/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === "started") {
      $("#screen-result").classList.add("hidden");
      $("#screen-summary").classList.add("hidden");
      startPolling();
    } else {
      $("#job-state").textContent = res.reason || "任务已在运行中";
    }
  } catch (e) {
    $("#job-state").textContent = `启动失败: ${e.message || e}`;
  }
});

function startPolling() {
  $("#run-btn").disabled = true;
  $("#job-progress").classList.remove("hidden");
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollJob, 1500);
  pollJob();
}

async function pollJob() {
  let job;
  try { job = await fetchJSON("/api/wb/job"); } catch { return; }
  if (job.state === "running") {
    $("#job-state").textContent = `筛选中... 已用 ${job.elapsed ?? 0}s`;
    $("#job-progress-hint").style.display = "block";
    return;
  }
  clearInterval(pollTimer);
  pollTimer = null;
  $("#job-progress").classList.add("hidden");
  $("#job-progress-hint").style.display = "none";
  $("#run-btn").disabled = false;
  if (job.state === "error") {
    $("#job-state").innerHTML = `<span class="down">失败: ${esc(job.error)}</span>`;
    return;
  }
  $("#job-state").textContent = `完成，耗时 ${job.elapsed}s`;
  if (job.timestamp) {
    const degraded = job.degraded ? " <span class='down'>⚠️ 数据降级</span>" : "";
    $("#screen-summary").innerHTML =
      `<div>数据时间: <strong>${esc(job.timestamp)}</strong>${degraded} ` +
      `｜ 报告已保存: <code>${esc(job.md_path || "")}</code></div>`;
    $("#screen-summary").classList.remove("hidden");
  }
  try {
    const md = await fetchText("/api/wb/report");
    $("#screen-result").innerHTML = renderMD(md);
    $("#screen-result").classList.remove("hidden");
  } catch (e) {
    $("#screen-result").innerHTML = `<p class="down">报告获取失败: ${esc(e.message || e)}</p>`;
    $("#screen-result").classList.remove("hidden");
  }
}

/* ================= 报告库 ================= */
async function loadReports() {
  const data = await fetchJSON("/api/wb/reports");
  $("#reports-count").textContent = `共 ${data.count} 份`;
  const rows = (data.files || []).map((f) =>
    `<tr><td><a data-path="${esc(f.path)}">${esc(f.name)}</a></td>` +
    `<td>${esc(f.mtime)}</td><td>${(f.size / 1024).toFixed(1)} KB</td></tr>`).join("");
  $("#reports-table").innerHTML =
    `<table class="data"><thead><tr><th>文件名</th><th>修改时间</th><th>大小</th></tr></thead>` +
    `<tbody>${rows || '<tr><td colspan="3">暂无报告</td></tr>'}</tbody></table>`;
  $$("#reports-table a[data-path]").forEach((a) => {
    a.addEventListener("click", () => openReport(a.dataset.path, a.textContent));
  });
}
$("#reports-refresh").addEventListener("click", loadReports);

async function openReport(path, name) {
  const md = await fetchText(`/api/wb/md?path=${encodeURIComponent(path)}`);
  $("#reports-table").closest(".card").classList.add("hidden");
  $("#report-viewer").classList.remove("hidden");
  $("#report-title").textContent = name;
  $("#report-content").innerHTML = renderMD(md);
}
$("#report-back").addEventListener("click", () => {
  $("#report-viewer").classList.add("hidden");
  $("#reports-table").closest(".card").classList.remove("hidden");
});

/* ================= 工具箱 ================= */
$("#quote-btn").addEventListener("click", () => {
  callTool($("#quote-btn"), "#quote-out", () => {
    const codes = $("#quote-codes").value.trim();
    const m = $("#quote-minute").checked ? 1 : 0;
    const k = $("#quote-kline").checked ? 1 : 0;
    return fetchJSON(`/api/wb/quote?codes=${encodeURIComponent(codes)}&minute=${m}&kline=${k}`);
  });
});

$("#fin-btn").addEventListener("click", () => {
  callTool($("#fin-btn"), "#fin-out", () =>
    fetchJSON(`/api/wb/financials?code=${encodeURIComponent($("#fin-code").value.trim())}`));
});

$("#scan-btn").addEventListener("click", () => {
  callTool($("#scan-btn"), "#scan-out", () => {
    const date = $("#scan-date").value.trim();
    const latest = parseInt($("#scan-latest").value, 10) || 5;
    const q = date ? `date=${encodeURIComponent(date)}&latest=${latest}` : `latest=${latest}`;
    return fetchJSON(`/api/wb/scan?${q}`);
  });
});

$("#pos-btn").addEventListener("click", () => {
  callTool($("#pos-btn"), "#pos-out", () => {
    const date = $("#pos-date").value.trim();
    return fetchJSON(`/api/wb/position${date ? `?date=${encodeURIComponent(date)}` : ""}`);
  });
});

$("#t1-btn").addEventListener("click", () => {
  callTool($("#t1-btn"), "#t1-out", () =>
    fetchJSON(`/api/wb/verify_t1?date=${encodeURIComponent($("#t1-date").value.trim())}`));
});

$("#track-btn").addEventListener("click", () => {
  callTool($("#track-btn"), "#track-out", () => {
    const code = $("#track-code").value.trim();
    const date = $("#track-date").value.trim();
    return fetchJSON(`/api/wb/track?code=${encodeURIComponent(code)}${date ? `&date=${encodeURIComponent(date)}` : ""}`);
  });
});

/* ================= 服务器状态与时钟 ================= */
async function ping() {
  try {
    const st = await fetchJSON("/api/status");
    const el = $("#server-status");
    el.textContent = "服务正常";
    el.className = "status-badge online";
  } catch {
    const el = $("#server-status");
    el.textContent = "连接断开";
    el.className = "status-badge offline";
  }
}
setInterval(ping, 10000);
ping();

setInterval(() => {
  $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);

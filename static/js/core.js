// 공통 유틸: API 호출, SSE, 렌더링 헬퍼
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, body, method) {
  const opts = { method: method || (body === undefined ? "GET" : "POST") };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers = { "Content-Type": "application/json" }; }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data));
  return data;
}

// fetch 기반 SSE 소비 (POST 바디 지원)
async function sse(path, body, handlers) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(d.detail || `HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      let ev = "message", data = "";
      for (const line of raw.split("\n")) {
        if (line.startsWith("event: ")) ev = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      const payload = data ? JSON.parse(data) : null;
      if (ev === "error") throw new Error(payload?.detail || "stream error");
      handlers[ev]?.(payload);
    }
  }
}

function usageHtml(u) {
  if (!u) return "";
  const parts = [];
  if (u.model) parts.push(esc(u.model));
  parts.push(`in ${(u.input_tokens || 0).toLocaleString()}`, `out ${(u.output_tokens || 0).toLocaleString()}`);
  if (u.cache_read_tokens) parts.push(`cache↺ ${u.cache_read_tokens.toLocaleString()}`);
  if (u.cache_write_tokens) parts.push(`cache✎ ${u.cache_write_tokens.toLocaleString()}`);
  parts.push(`$${(u.cost_usd || 0).toFixed(4)}`);
  return `<div class="usage">${parts.map(p => `<span>${p}</span>`).join("")}</div>`;
}

const errHtml = e => `<div class="err">⚠ ${esc(e.message || e)}</div>`;
const loadingHtml = (t = "처리 중…") => `<div class="muted small"><span class="spinner"></span> ${esc(t)}</div>`;

// 버튼 잠금 + 로딩/에러 처리
async function run(btn, outEl, fn, loadingText) {
  btn.disabled = true;
  if (outEl) outEl.innerHTML = loadingHtml(loadingText);
  try { await fn(); }
  catch (e) { if (outEl) outEl.innerHTML = errHtml(e); else alert(e.message); }
  finally { btn.disabled = false; }
}

function table(cols, rows, opts = {}) {
  const head = cols.map(c => `<th class="${c.num ? "num" : ""}">${esc(c.label ?? c.key ?? c)}</th>`).join("");
  const body = rows.map((r, i) => {
    const cls = opts.rowClass ? opts.rowClass(r, i) : "";
    return `<tr class="${cls}">` + cols.map(c => {
      const key = c.key ?? c;
      const v = Array.isArray(r) ? r[cols.indexOf(c)] : r[key];
      const html = c.render ? c.render(v, r) : esc(v);
      return `<td class="${c.num ? "num" : ""}">${html}</td>`;
    }).join("") + "</tr>";
  }).join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// 아주 작은 마크다운 렌더러 (헤딩, 목록, 굵게, 코드, 링크)
function md(src) {
  const lines = esc(src).split("\n");
  let html = "", list = null, code = false;
  const inline = s => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const l of lines) {
    if (l.startsWith("```")) { close(); html += code ? "</code></pre>" : "<pre><code>"; code = !code; continue; }
    if (code) { html += l + "\n"; continue; }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { close(); const n = Math.min(m[1].length + 1, 4); html += `<h${n}>${inline(m[2])}</h${n}>`; }
    else if ((m = l.match(/^\s*[-*]\s+(.*)/))) { if (list !== "ul") { close(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { close(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
    else if (!l.trim()) { close(); }
    else { close(); html += `<p>${inline(l)}</p>`; }
  }
  close();
  if (code) html += "</code></pre>";
  return `<div class="md">${html}</div>`;
}

// SVG 차트 (막대/선)
function chart(type, labels, values, { height = 240 } = {}) {
  const W = 640, H = height, pad = { l: 64, r: 12, t: 12, b: 48 };
  const max = Math.max(...values, 0) || 1;
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const x = i => pad.l + (iw * (i + 0.5)) / labels.length;
  const y = v => pad.t + ih - (ih * v) / max;
  const fmt = v => v >= 1e8 ? (v / 1e8).toFixed(1) + "억" : v >= 1e4 ? (v / 1e4).toFixed(0) + "만" : (+v).toLocaleString();
  let g = "";
  for (let k = 0; k <= 4; k++) {
    const v = (max * k) / 4;
    g += `<line class="axis" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text>`;
  }
  const step = Math.ceil(labels.length / 12);
  labels.forEach((l, i) => {
    if (i % step === 0) g += `<text x="${x(i)}" y="${H - pad.b + 16}" text-anchor="middle">${esc(String(l).slice(0, 10))}</text>`;
  });
  if (type === "line") {
    g += `<polyline class="line" points="${values.map((v, i) => `${x(i)},${y(v)}`).join(" ")}"/>`;
    values.forEach((v, i) => (g += `<circle class="mark" cx="${x(i)}" cy="${y(v)}" r="3"><title>${esc(labels[i])}: ${v.toLocaleString()}</title></circle>`));
  } else {
    const bw = Math.max(4, (iw / labels.length) * 0.6);
    values.forEach((v, i) => (g += `<rect class="mark" x="${x(i) - bw / 2}" y="${y(v)}" width="${bw}" height="${pad.t + ih - y(v)}" rx="2"><title>${esc(labels[i])}: ${v.toLocaleString()}</title></rect>`));
  }
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" width="100%" role="img">${g}</svg>`;
}

function bars(items, { max = 1, fmt = v => (v * 100).toFixed(0) + "%" } = {}) {
  return `<div class="bars">${items.map(it => `<div class="bar"><span>${esc(it.label)}</span><div class="track"><div class="fill" style="width:${Math.max(0, Math.min(100, (it.value / max) * 100))}%"></div></div><span class="mono">${fmt(it.value)}</span></div>`).join("")}</div>`;
}

function tabs(container, items) {
  container.innerHTML = `<div class="tabs">${items.map((t, i) => `<button data-i="${i}" class="${i ? "" : "active"}">${esc(t.label)}</button>`).join("")}</div><div class="tab-body"></div>`;
  const body = $(".tab-body", container);
  const show = i => {
    $$(".tabs button", container).forEach(b => b.classList.toggle("active", +b.dataset.i === i));
    body.innerHTML = "";
    items[i].render(body);
  };
  $$(".tabs button", container).forEach(b => (b.onclick = () => show(+b.dataset.i)));
  show(0);
}

const DEMOS = {}; // 프로젝트 id → render(el)

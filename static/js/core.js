// 공통 유틸: API 호출, SSE, 렌더링 헬퍼
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 화면 전환(app.js route) 시 진행 중인 요청을 끊는다. SSE는 서버도 다음 이벤트에서 멈추므로 공유 예산이 덜 샌다.
let NAV_CTRL = new AbortController();
function abortNav() { NAV_CTRL.abort(); NAV_CTRL = new AbortController(); }
const isAbort = e => e && e.name === "AbortError";

// 오류 응답 → 읽을 수 있는 한국어 메시지 (앱의 문자열 detail이 항상 우선)
function errMsg(data, res) {
  const d = data && data.detail;
  if (typeof d === "string" && d) return d;
  if (Array.isArray(d) && d.length) // FastAPI 422: [{loc, msg}]
    return "입력값 오류: " + d.map(x => { const f = (x.loc || []).filter(p => p !== "body").join("."); return f ? `${f}: ${x.msg}` : x.msg; }).join(" / ");
  const s = res ? res.status : 0;
  if (s === 502 || s === 503 || s === 504) return `서버가 재시작 중이거나 잠에서 깨어나는 중입니다 (HTTP ${s}). 30초~1분 뒤 다시 시도해 주세요.`;
  if (s === 413) return "업로드한 파일이 너무 큽니다.";
  if (s >= 500) return `서버 내부 오류가 발생했습니다 (HTTP ${s}).`;
  return `요청이 실패했습니다 (HTTP ${s}).`;
}

async function request(path, opts) {
  try { return await fetch(path, { ...opts, signal: NAV_CTRL.signal }); }
  catch (e) { if (isAbort(e)) throw e; throw new Error("네트워크 오류로 서버에 연결하지 못했습니다."); }
}

function httpError(data, res) {
  const err = new Error(errMsg(data, res));
  err.status = res.status;
  return err;
}

async function api(path, body, method) {
  const opts = { method: method || (body === undefined ? "GET" : "POST") };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers = { "Content-Type": "application/json" }; }
  const res = await request(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { if (isAbort(e)) throw e; }
  if (!res.ok) throw httpError(data, res);
  if (data === null) throw new Error("서버 응답을 해석하지 못했습니다. 잠시 후 다시 시도해 주세요.");
  return data;
}

// fetch 기반 SSE 소비 (POST 바디 지원). done 이벤트 없이 끝나면 잘린 응답으로 보고 예외를 던진다.
async function sse(path, body, handlers) {
  try {
    const res = await request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) {
      const d = await res.json().catch(e => { if (isAbort(e)) throw e; return null; });
      throw httpError(d, res);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "", gotDone = false;
    try {
      for (;;) {
        let chunk;
        try { chunk = await reader.read(); }
        catch (e) { if (isAbort(e)) throw e; throw new Error("스트림 연결이 끊겼습니다. 결과가 일부만 표시되었을 수 있습니다."); }
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const raw = buf.slice(0, i); buf = buf.slice(i + 2);
          let ev = "message", data = "";
          for (const line of raw.split("\n")) {
            if (line.startsWith("event: ")) ev = line.slice(7);
            else if (line.startsWith("data: ")) data += line.slice(6);
          }
          const payload = data ? JSON.parse(data) : null;
          if (ev === "error") throw new Error((payload && typeof payload.detail === "string" && payload.detail) || "스트림 처리 중 서버 오류가 발생했습니다.");
          if (ev === "done") gotDone = true;
          if (handlers[ev]) handlers[ev](payload);
        }
      }
    } finally { reader.cancel().catch(() => {}); } // 핸들러 예외·중단 시에도 연결을 닫는다
    if (!gotDone) throw new Error("응답 스트림이 중간에 끊겼습니다(서버 재시작/네트워크). 결과가 일부만 표시되었을 수 있습니다.");
  } finally {
    if (typeof refreshStatus === "function") refreshStatus(); // app.js: 남은 예산 갱신
  }
}

// number input의 min/max는 직접 타이핑한 값을 막지 못하므로, 전송 전에 범위로 고정하고 화면에도 반영한다
function numVal(input) { // 화면은 건드리지 않고 전송될 값만 계산 (호출 수 안내용)
  const lo = input.min === "" ? -Infinity : +input.min, hi = input.max === "" ? Infinity : +input.max;
  let v = parseFloat(input.value);
  if (!Number.isFinite(v)) v = parseFloat(input.defaultValue);
  if (!Number.isFinite(v)) v = Number.isFinite(lo) ? lo : 0;
  return Math.min(hi, Math.max(lo, Math.round(v)));
}
function num(input) {
  const v = numVal(input);
  input.value = v;
  return v;
}

// 업로드 이미지 전처리: 큰 사진은 긴 변 maxEdge px JPEG로 줄여 API 5MB 제한·업로드 시간·서버 메모리를 아낀다
// (API 이미지 한도 5MB는 base64 기준이라 원본은 약 3.7MB가 상한 → 3.5MiB 이하로 맞춘다)
const IMG_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const IMG_MAX_BYTES = 3.5 * 1024 * 1024;
async function prepImage(file, maxEdge = 2576) {
  if (file.type && !file.type.startsWith("image/")) throw new Error("이미지 파일만 올릴 수 있습니다.");
  const known = IMG_TYPES.includes(file.type);
  if (known && file.size <= IMG_MAX_BYTES) return file; // 스크린샷 등은 원본(PNG 선명도) 유지
  if (typeof createImageBitmap !== "function") {
    if (known) return file; // 서버가 크기를 검사해 안내한다
    throw new Error("이 브라우저에서 열 수 없는 이미지 형식입니다. JPG/PNG로 변환해 주세요.");
  }
  let bmp;
  try { bmp = await createImageBitmap(file, { imageOrientation: "from-image" }); }
  catch (e) { throw new Error("HEIC 등 이 브라우저에서 열 수 없는 형식입니다. JPG/PNG로 변환해 주세요."); }
  try {
    // 세밀한 사진은 한 번에 한도 안에 안 들어갈 수 있어 해상도·품질을 단계적으로 낮춘다
    for (const [edge, q] of [[maxEdge, 0.85], [maxEdge * 0.75, 0.8], [maxEdge * 0.5, 0.75]]) {
      const scale = Math.min(1, edge / Math.max(bmp.width, bmp.height));
      const w = Math.max(1, Math.round(bmp.width * scale)), h = Math.max(1, Math.round(bmp.height * scale));
      const cv = document.createElement("canvas");
      cv.width = w; cv.height = h;
      const ctx = cv.getContext("2d");
      ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, w, h); // 투명 PNG → JPEG 시 검은 배경 방지
      ctx.drawImage(bmp, 0, 0, w, h);
      const blob = await new Promise(r => cv.toBlob(r, "image/jpeg", q));
      if (!blob) break;
      if (blob.size <= IMG_MAX_BYTES) return new File([blob], (file.name || "image").replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
    }
  } finally { if (bmp.close) bmp.close(); }
  throw new Error("이미지를 업로드 가능한 크기로 줄이지 못했습니다. 다른 파일로 시도해 주세요.");
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

// 버튼 잠금 + 로딩(경과 시간)/에러 처리
async function run(btn, outEl, fn, loadingText) {
  btn.disabled = true;
  let timer = null;
  if (outEl) {
    outEl.innerHTML = `<div class="muted small"><span class="spinner"></span> ${esc(loadingText || "처리 중…")} <span class="mono elapsed"></span></div>`;
    const t0 = Date.now();
    timer = setInterval(() => { // 오래 걸리는 호출이 멈춘 것처럼 보이지 않도록
      const s = Math.round((Date.now() - t0) / 1000), x = $(".elapsed", outEl);
      if (x && s >= 2) x.textContent = `${s}초`;
    }, 1000);
  }
  try { await fn(); }
  catch (e) {
    if (isAbort(e)) return; // 다른 화면으로 이동해 취소된 요청
    if (outEl) outEl.innerHTML = errHtml(e); else alert(e.message);
  }
  finally {
    clearInterval(timer);
    btn.disabled = false;
    if (typeof refreshStatus === "function") refreshStatus(); // app.js: 남은 예산 갱신 (버스트도 한 번만)
  }
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
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer nofollow">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer nofollow">$2</a>');
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const l of lines) {
    if (l.startsWith("```")) { close(); html += code ? "</code></pre>" : "<pre><code>"; code = !code; continue; }
    if (code) { html += l + "\n"; continue; }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { close(); const n = Math.min(m[1].length + 1, 4); html += `<h${n}>${inline(m[2])}</h${n}>`; }
    else if ((m = l.match(/^\s*[-*]\s+(.*)/))) { if (list !== "ul") { close(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^\s*(\d+)[.)]\s+(.*)/))) { // 하위 목록·빈 줄로 끊겨도 원래 번호부터 이어지게 start 지정
      if (list !== "ol") { close(); html += m[1] === "1" ? "<ol>" : `<ol start="${+m[1]}">`; list = "ol"; }
      html += `<li>${inline(m[2])}</li>`;
    }
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
  // 탭마다 새 pane을 준다: 늦게 끝난 비동기 render가 다른 탭 화면을 덮어쓰지 않고, 실패하면 오류를 보여준다
  const show = i => {
    $$(".tabs button", container).forEach(b => b.classList.toggle("active", +b.dataset.i === i));
    const pane = document.createElement("div");
    pane.innerHTML = loadingHtml("불러오는 중…");
    body.innerHTML = "";
    body.appendChild(pane);
    Promise.resolve().then(() => items[i].render(pane)).catch(e => { if (pane.isConnected && !isAbort(e)) pane.innerHTML = errHtml(e); });
  };
  $$(".tabs button", container).forEach(b => (b.onclick = () => show(+b.dataset.i)));
  show(0);
}

const DEMOS = {}; // 프로젝트 id → render(el)

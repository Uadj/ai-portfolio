// 라우터 + 홈 + 프로젝트 페이지
let STATUS = {}; // /api/status 결과. 비어 있으면 아직 못 받았거나 조회 실패

// 서버 예산 검사와 같은 기준(spent >= budget)
function budgetOf(s) {
  const budget = s.daily_budget_usd || 0, spent = s.spent_today_usd || 0;
  return { budget, left: Math.max(0, budget - spent), exhausted: !!budget && spent >= budget };
}
const usd = v => "$" + (Number.isInteger(v) ? v : v.toFixed(2));

function paintStatus(failed) {
  const el = $("#llm-status"), s = STATUS, b = budgetOf(s);
  let cls = "ok", text;
  if (failed) { cls = "warn"; text = "● 상태 확인 실패"; }
  else if (!s.llm_ready) { cls = "warn"; text = "● API 키 미설정"; }
  else if (b.exhausted) { cls = "bad"; text = "● 오늘 데모 예산 소진"; }
  else if (b.budget) { cls = b.left < b.budget * 0.2 ? "warn" : "ok"; text = `● 오늘 남은 예산 $${b.left.toFixed(2)} / ${usd(b.budget)}`; }
  else text = "● API 키 설정됨"; // 키 존재만 확인한 것이라 '연결됨'이라고 하지 않는다
  el.className = "pill " + cls;
  el.textContent = text;
  const tips = failed ? [] : [
    s.rate_per_hour ? `IP당 시간당 ${s.rate_per_hour}회 요청 제한` : "",
    b.budget && s.budget_scope === "process" ? "사용액은 서버 메모리 기준이라 서버가 재시작되면 초기화됩니다" : "",
  ].filter(Boolean);
  if (tips.length) el.title = tips.join(" · ");
  else el.removeAttribute("title");
}

// core.js의 run()/sse()가 끝날 때마다 호출된다. 동시에 여러 번 불려도 요청은 하나만 보낸다.
let statusReq = null;
function refreshStatus() {
  if (statusReq) return statusReq;
  statusReq = fetch("/api/status", { cache: "no-store" })
    .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then(s => { STATUS = s || {}; paintStatus(false); })
    .catch(() => { if (STATUS.llm_ready === undefined) paintStatus(true); }) // 이전에 받은 상태가 있으면 유지
    .finally(() => { statusReq = null; });
  return statusReq;
}

function home(app) {
  let filter = "all";
  try { filter = sessionStorage.getItem("lv") || "all"; } catch {}
  const offline = PROJECTS.filter(p => p.offline).length;
  const limits = [STATUS.daily_budget_usd ? `하루 Claude 사용 예산(${usd(STATUS.daily_budget_usd)})` : "", STATUS.rate_per_hour ? `IP당 시간당 요청 수(${STATUS.rate_per_hour}회)` : ""].filter(Boolean);
  app.innerHTML = `
    <section class="hero">
      <h1>직접 만들고, 측정하고, 검증한<br>AI 엔지니어링 프로젝트 15선</h1>
      <p>LLM API 기초부터 RAG, 에이전트, 평가, 파인튜닝, 프로덕션 게이트웨이와 보안까지 다룹니다. 대부분의 프로젝트는 이 사이트에서 바로 실행해 볼 수 있습니다. #12 학습·평가와 #13 벤치마크는 로컬 GPU/Ollama 환경이 필요합니다.</p>
      ${limits.length ? `<p class="small muted" style="margin-top:8px">공개 데모라 ${limits.join("과 ")}에 상한이 있습니다. 호출이 많은 데모는 실행 버튼 옆에 Claude 호출 횟수를 적어 두었습니다.</p>` : ""}
    </section>
    <div class="stats-row">
      <div class="stat"><b>${PROJECTS.filter(p => !p.local).length}</b><span>사이트에서 실행 가능한 데모</span></div>
      <div class="stat"><b>4</b><span>난이도 레벨</span></div>
      <div class="stat"><b>${offline}</b><span>API 키 없이 측정 가능</span></div>
      <div class="stat"><b>${esc(STATUS.model || "claude-opus-5-5")}</b><span>기본 모델</span></div>
    </div>
    <div class="filters">${[["all", "전체"], ...Object.entries(LEVELS).map(([k, v]) => [k, v.name])].map(([k, v]) => `<button class="chip ${filter === k ? "active" : ""}" data-lv="${k}">${v}</button>`).join("")}</div>
    <div id="list"></div>`;
  const draw = lv => {
    $("#list").innerHTML = Object.entries(LEVELS).filter(([k]) => lv === "all" || lv === k).map(([k, L]) => `
      <div class="level-title"><h2>${L.name}</h2><span>${L.desc}</span></div>
      <div class="grid">${PROJECTS.filter(p => p.level == k).map(card).join("")}</div>`).join("");
  };
  $$(".chip").forEach(c => (c.onclick = () => {
    $$(".chip").forEach(x => x.classList.toggle("active", x === c));
    try { sessionStorage.setItem("lv", c.dataset.lv); } catch {}
    draw(c.dataset.lv);
  }));
  draw(filter);
}

const card = p => `<a class="card" href="#/p/${p.id}">
  <div class="card-top"><span class="num">#${p.id}</span><span class="lv lv${p.level}">L${p.level}</span>${p.offline ? '<span class="badge-offline">키 없이 일부 동작</span>' : p.local ? '<span class="badge-local">로컬 전용 (Ollama 필요)</span>' : ""}</div>
  <h3>${esc(p.title)}</h3><p>${esc(p.one)}</p>
  <div class="tags">${p.tags.map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div></a>`;

function project(app, id) {
  const p = PROJECTS.find(x => x.id === id);
  if (!p) return (app.innerHTML = `<p>프로젝트를 찾을 수 없습니다. <a href="#/">홈으로</a></p>`);
  const i = PROJECTS.indexOf(p), prev = PROJECTS[i - 1], next = PROJECTS[i + 1];
  app.innerHTML = `
    <div class="crumb"><a href="#/">프로젝트</a> / ${LEVELS[p.level].name}</div>
    <div class="proj-head"><span class="lv lv${p.level}">Level ${p.level}</span> <span class="num mono muted">#${p.id}</span>
      <h1>${esc(p.title)}</h1><p>${esc(p.one)}</p><div class="tags">${p.tags.map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div></div>
    <div class="cols">
      <div class="panel"><h3>라이브 데모</h3>
        ${demoNote(p)}
        <div id="demo"></div></div>
      <aside class="panel side">
        <section><h3>검증하는 역량</h3><ul>${p.proves.map(x => `<li>${esc(x)}</li>`).join("")}</ul></section>
        <section><h3>구현 포인트</h3><ul>${p.points.map(x => `<li>${esc(x)}</li>`).join("")}</ul></section>
        <section class="files"><h3>코드</h3>${p.files.map(f => `<code>${esc(f)}</code>`).join("")}</section>
        <section class="small">${prev ? `<a href="#/p/${prev.id}">← #${prev.id} ${esc(prev.title)}</a><br>` : ""}${next ? `<a href="#/p/${next.id}">#${next.id} ${esc(next.title)} →</a>` : ""}</section>
      </aside>
    </div>`;
  const demo = $("#demo");
  demo.innerHTML = loadingHtml("불러오는 중…");
  Promise.resolve().then(() => DEMOS[id](demo)).catch(e => { if (!isAbort(e)) demo.innerHTML = errHtml(e); });
}

// 상태 조회에 성공했을 때만 안내한다 (조회 실패를 '키 없음'으로 오해하지 않도록)
function demoNote(p) {
  if (p.local) return ""; // #13은 Claude 대신 로컬 Ollama를 쓰며, 데모가 자체 안내를 보여준다
  if (STATUS.llm_ready === false)
    return `<div class="note">Claude API 키가 설정되지 않았습니다. ${p.offline ? "이 프로젝트는 LLM 없이 동작하는 기능(측정·검색·실행)을 먼저 사용해 볼 수 있습니다." : "LLM 호출이 필요한 기능은 오류를 반환합니다."} <code>ai-portfolio/.env</code>에 <code>ANTHROPIC_API_KEY</code>를 넣고 서버를 재시작하세요.</div>`;
  if (STATUS.llm_ready && budgetOf(STATUS).exhausted)
    return `<div class="note">오늘 공개 데모 예산(${usd(STATUS.daily_budget_usd)})을 모두 사용해 Claude를 호출하는 기능은 멈춰 있습니다. ${p.offline ? "LLM 없이 동작하는 측정·검색 기능은 계속 쓸 수 있습니다." : "내일 다시 시도해 주세요."}</div>`;
  return "";
}

function about(app) {
  app.innerHTML = `<div class="proj-head"><h1>아키텍처</h1><p>모든 프로젝트는 FastAPI 서버 하나와 프로젝트별 라우터 모듈, 공통 LLM 래퍼로 구성됩니다.</p></div>
  <div class="panel" style="margin-top:16px">${md(`## 구조
\`\`\`
ai-portfolio/
├─ server.py              FastAPI 앱 · projects/*의 router 자동 등록 · 정적 파일 서빙
├─ core/llm.py            Claude 호출 래퍼 (모델·effort·refusal fallback·사용량/비용 계산)
├─ core/sse.py            Server-Sent Events 헬퍼
├─ core/net.py            레이트 리밋용 클라이언트 IP 판별
├─ projects/p01~p15_*.py  프로젝트별 API
├─ evals/                 #11 Eval 엔진 + 프롬프트 버전
├─ mcp_server/            #09 MCP 서버 (stdio)
├─ p12_finetune/          #12 데이터 합성 / LoRA 학습 / 평가 스크립트
├─ scripts/run_eval.py    CI용 Eval 실행기
├─ .github/workflows/     프롬프트 변경 시 Eval 게이트
├─ data/                  사내 문서(가상), 평가셋, 공격셋, SQLite DB
└─ static/                이 사이트 (빌드 없는 바닐라 JS)
\`\`\`
## 공통 설계 원칙
- **측정 먼저**: RAG Recall@k, Eval 점수, 방어율, 처리량처럼 모든 프로젝트가 숫자를 냅니다
- **비용 투명성**: 모든 응답에 토큰 수와 달러 비용을 표시합니다
- **안전한 기본값**: 읽기 전용 SQL, 웹훅 서명 검증, 카나리 토큰, 루프 반복 상한
- **구조화 출력**: 파싱이 필요한 응답은 모두 Pydantic 스키마로 강제합니다
- **정직한 데모**: 실행하지 않은 결과(예: 파인튜닝 수치)는 지어내지 않고 비워 둡니다
- **공개 데모 보호**: 일일 예산·IP별 요청 제한, 업로드 크기 제한, 화면을 떠나면 진행 중인 스트림 중단
## 실행
\`\`\`
pip install -r requirements.txt
cp .env.example .env      # ANTHROPIC_API_KEY 입력
python server.py          # http://localhost:8000
\`\`\``)}</div>`;
}

function route() {
  abortNav(); // 이전 화면의 진행 중인 요청(SSE 포함)을 끊어 아무도 보지 않는 결과에 예산을 쓰지 않는다
  const app = $("#app");
  const h = location.hash.replace(/^#/, "") || "/";
  const m = h.match(/^\/p\/(\d{2})/);
  if (m) project(app, m[1]);
  else if (h === "/about") about(app);
  else home(app);
  window.scrollTo(0, 0);
}

// 테마
(function initTheme() {
  let t = null;
  try { t = localStorage.getItem("theme"); } catch {}
  if (t) document.documentElement.dataset.theme = t;
  $("#theme-btn").onclick = () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const nt = dark ? "light" : "dark";
    document.documentElement.dataset.theme = nt;
    try { localStorage.setItem("theme", nt); } catch {}
  };
})();

window.addEventListener("hashchange", route);
// 상태를 받은 뒤 첫 화면을 그리되, 응답이 늦으면 3초 뒤 그냥 그린다
Promise.race([refreshStatus(), new Promise(r => setTimeout(r, 3000))]).then(route);

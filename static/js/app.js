// 라우터 + 홈 + 프로젝트 페이지
let STATUS = { llm_ready: false };

function home(app) {
  const filter = sessionStorage.getItem("lv") || "all";
  const offline = PROJECTS.filter(p => p.offline).length;
  app.innerHTML = `
    <section class="hero">
      <h1>직접 만들고, 측정하고, 검증한<br>AI 엔지니어링 프로젝트 15선</h1>
      <p>LLM API 기초부터 RAG, 에이전트, 평가, 파인튜닝, 프로덕션 게이트웨이와 보안까지 다룹니다. 모든 프로젝트는 이 사이트에서 바로 실행해 볼 수 있습니다.</p>
    </section>
    <div class="stats-row">
      <div class="stat"><b>15</b><span>실행 가능한 데모</span></div>
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
  <div class="card-top"><span class="num">#${p.id}</span><span class="lv lv${p.level}">L${p.level}</span>${p.offline ? '<span class="badge-offline">키 없이 일부 동작</span>' : ""}</div>
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
        ${!STATUS.llm_ready ? `<div class="note">Claude API 키가 설정되지 않았습니다. ${p.offline ? "이 프로젝트는 LLM 없이 동작하는 기능(측정·검색·실행)을 먼저 사용해 볼 수 있습니다." : "LLM 호출이 필요한 기능은 오류를 반환합니다."} <code>ai-portfolio/.env</code>에 <code>ANTHROPIC_API_KEY</code>를 넣고 서버를 재시작하세요.</div>` : ""}
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
  Promise.resolve().then(() => DEMOS[id](demo)).catch(e => (demo.innerHTML = errHtml(e)));
}

function about(app) {
  app.innerHTML = `<div class="proj-head"><h1>아키텍처</h1><p>모든 프로젝트는 FastAPI 서버 하나와 프로젝트별 라우터 모듈, 공통 LLM 래퍼로 구성됩니다.</p></div>
  <div class="panel" style="margin-top:16px">${md(`## 구조
\`\`\`
ai-portfolio/
├─ server.py              FastAPI 앱 · projects/*의 router 자동 등록 · 정적 파일 서빙
├─ core/llm.py            Claude 호출 래퍼 (모델·effort·refusal fallback·사용량/비용 계산)
├─ core/sse.py            Server-Sent Events 헬퍼
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
## 실행
\`\`\`
pip install -r requirements.txt
cp .env.example .env      # ANTHROPIC_API_KEY 입력
python server.py          # http://localhost:8000
\`\`\``)}</div>`;
}

function route() {
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
api("/api/status").then(s => {
  STATUS = s;
  const el = $("#llm-status");
  el.className = "pill " + (s.llm_ready ? "ok" : "warn");
  el.textContent = s.llm_ready ? "● Claude API 연결됨" : "● API 키 미설정";
}).catch(() => {}).finally(route);

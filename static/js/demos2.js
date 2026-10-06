// Level 2–3 데모: #06 ~ #10

// #06 코드 리뷰
const DIFF_SAMPLE = `diff --git a/app/users.py b/app/users.py
--- a/app/users.py
+++ b/app/users.py
@@ -1,12 +1,24 @@
 import sqlite3
+import pickle
+import requests

 DB = "app.db"


-def get_user(user_id):
-    con = sqlite3.connect(DB)
-    return con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
+def get_user(user_id):
+    con = sqlite3.connect(DB)
+    return con.execute(f"SELECT * FROM users WHERE id = {user_id}").fetchone()
+
+
+def load_session(raw: bytes):
+    return pickle.loads(raw)
+
+
+def get_users_with_orders(ids):
+    result = []
+    for i in ids:
+        user = get_user(i)
+        orders = requests.get(f"http://orders.internal/api?user={i}").json()
+        result.append({"user": user, "orders": orders})
+    return result


 def average(values):
-    return sum(values) / len(values) if values else 0
+    return sum(values) / len(values)
`;

DEMOS["06"] = el => {
  el.innerHTML = `
    <label>Unified diff (git diff 출력)</label><textarea id="d" class="code">${esc(DIFF_SAMPLE)}</textarea>
    <div class="actions"><button class="btn" id="go">리뷰 요청</button><span class="muted small">샘플에는 SQL 인젝션, 안전하지 않은 역직렬화, N+1 호출, 0 나누기가 숨어 있습니다</span></div>
    <div class="out" id="o"></div>
    <details><summary>GitHub PR 자동 리뷰 연결 방법</summary><div class="box small">
      1) <code>.env</code>에 <code>GITHUB_TOKEN</code>, <code>GITHUB_WEBHOOK_SECRET</code> 설정<br>
      2) 저장소 Settings → Webhooks → Payload URL: <code>https://&lt;공개주소&gt;/api/p06/github-webhook</code>, Content type: <code>application/json</code>, 이벤트: Pull requests<br>
      3) PR이 열리거나 커밋이 푸시되면 서명을 검증한 뒤 인라인 리뷰를 게시합니다</div></details>`;
  $("#go", el).onclick = e => run(e.target, $("#o", el), async () => {
    const r = await api("/api/p06/review", { diff: $("#d", el).value });
    const byLine = {};
    r.comments.forEach(c => { const k = `${c.file}:${c.line}`; (byLine[k] = byLine[k] || []).push(c); }); // ES2020 호환 (논리 할당 연산자 미사용)
    const verdict = { approve: ["okbox", "승인"], request_changes: ["err", "변경 요청"], comment: ["note", "코멘트"] }[r.verdict];
    let html = `<div class="${verdict[0]}"><b>${verdict[1]}</b> — ${esc(r.summary)}</div><div class="diff" style="margin-top:12px">`;
    for (const f of r.files) {
      html += `<div class="f">${esc(f.file)}</div>`;
      for (const h of f.hunks) for (const l of h.lines) {
        const mark = { add: "+", del: "-", ctx: " " }[l.type];
        html += `<div class="l ${l.type}"><span>${l.new ?? ""}</span><span>${mark}</span><span>${esc(l.text)}</span></div>`;
        for (const c of (l.new != null && byLine[`${f.file}:${l.new}`]) || [])
          html += `<div class="cm"><span class="sev ${c.severity}">${c.severity}</span><b>${esc(c.category)}</b> ${esc(c.comment)}
            ${c.suggestion ? `<pre class="box" style="margin-top:6px">${esc(c.suggestion)}</pre>` : ""}</div>`;
      }
    }
    html += "</div>";
    if (r.dropped_invalid_line.length) html += `<div class="note">diff에 없는 줄을 가리킨 코멘트 ${r.dropped_invalid_line.length}개를 걸러냈습니다.</div>`;
    $("#o", el).innerHTML = html + usageHtml(r.usage);
  }, "리뷰 중…");
};

// #07 멀티모달
const MEETING_SAMPLE = `[00:00] 김지현(PM): 다음 달 고객지원 챗봇 출시 건 점검하겠습니다. 현재 RAG 정확도가 어느 정도죠?
[00:12] 박민수(ML): 평가셋 기준 Recall@3이 78%예요. 청킹을 헤딩 기반으로 바꾸면 85%까지 갈 것 같습니다.
[00:30] 김지현(PM): 좋아요. 그건 민수 님이 다음 주 금요일까지 실험해서 공유해 주세요.
[00:41] 이수진(BE): 응답 지연이 p95 기준 6초라서 스트리밍 적용이 필요합니다. 제가 이번 주 안에 SSE로 바꿀게요.
[01:05] 박민수(ML): 프롬프트 인젝션 테스트는 아직 안 했어요. 보안팀이랑 일정 잡아야 할 것 같아요.
[01:15] 김지현(PM): 출시일은 11월 3일로 확정하겠습니다. 인젝션 테스트 일정은 제가 보안팀과 조율할게요.
[01:30] 이수진(BE): 비용 대시보드는 출시 이후로 미루는 게 어떨까요?
[01:38] 김지현(PM): 동의합니다. 출시 후 2주 안에 착수하는 걸로 하죠. 혹시 다국어 지원은 1차 범위에 넣어야 할까요?
[01:50] 박민수(ML): 그건 고객 데이터 보고 다시 판단하면 좋겠습니다.`;

DEMOS["07"] = el => tabs(el, [
  { label: "이미지 분석", render: b => {
    b.innerHTML = `<label>이미지 (png/jpg/gif/webp · 큰 사진은 브라우저에서 자동 축소)</label><input type="file" id="f" accept="image/*">
      <div id="pv" style="margin-top:8px"></div>
      <label>추가 질문 (선택)</label><input type="text" id="q" maxlength="500" placeholder="예: 이 사진에서 개선할 점은?">
      <div class="actions"><button class="btn" id="go">분석</button></div><div class="out" id="o"></div>`;
    let preview = null; // 이전 미리보기 blob URL은 해제해 메모리를 돌려준다
    $("#f", b).onchange = () => {
      const f = $("#f", b).files[0];
      if (preview) { URL.revokeObjectURL(preview); preview = null; }
      $("#pv", b).innerHTML = "";
      if (f) { preview = URL.createObjectURL(f); $("#pv", b).innerHTML = `<img src="${preview}" alt="업로드한 이미지 미리보기" style="max-width:100%;max-height:260px;border-radius:8px">`; }
    };
    $("#go", b).onclick = e => run(e.target, $("#o", b), async () => {
      const f = $("#f", b).files[0];
      if (!f) throw new Error("이미지를 선택하세요.");
      const fd = new FormData(); fd.append("file", await prepImage(f)); fd.append("question", $("#q", b).value);
      const r = await api("/api/p07/image", fd); const a = r.analysis;
      $("#o", b).innerHTML = `<div class="box"><dl class="kv">
        <dt>캡션</dt><dd>${esc(a.caption)}</dd><dt>분류</dt><dd>${esc(a.category)}</dd>
        <dt>객체</dt><dd>${a.objects.map(o => `${esc(o.name)} ×${o.count}`).join(", ") || "-"}</dd>
        <dt>텍스트(OCR)</dt><dd>${a.text_in_image.map(esc).join(" / ") || "-"}</dd>
        <dt>대체 텍스트</dt><dd>${esc(a.alt_text)}</dd><dt>태그</dt><dd>${a.tags.map(t => `<span class="tag">${esc(t)}</span>`).join(" ")}</dd>
        <dt>주의 요소</dt><dd>${a.safety_flags.map(esc).join(", ") || "없음"}</dd></dl></div>
        ${r.answer ? `<div class="box">${md(r.answer)}</div>` : ""}${usageHtml(r.usage)}`;
    });
  } },
  { label: "회의록 생성", render: b => {
    b.innerHTML = `<label>회의 전사본 (STT 결과)</label><textarea id="t" class="code">${esc(MEETING_SAMPLE)}</textarea>
      <div class="actions"><button class="btn" id="go">회의록 생성</button></div><div class="out" id="o"></div>`;
    $("#go", b).onclick = e => run(e.target, $("#o", b), async () => {
      const r = await api("/api/p07/meeting", { transcript: $("#t", b).value }); const m = r.minutes;
      $("#o", b).innerHTML = `<div class="box"><h3 style="margin:0 0 6px">${esc(m.title)}</h3><p>${esc(m.summary)}</p>
        <h4>결정사항</h4><ul>${m.decisions.map(d => `<li>${esc(d)}</li>`).join("")}</ul>
        <h4>미결 질문</h4><ul>${m.open_questions.map(d => `<li>${esc(d)}</li>`).join("") || "<li>없음</li>"}</ul></div>
        <h4 style="margin:12px 0 6px">액션 아이템</h4>${table([{ key: "owner", label: "담당" }, { key: "task", label: "할 일" }, { key: "due", label: "기한", render: v => esc(v ?? "미정") }, { key: "priority", label: "우선순위" }], m.action_items)}
        <h4 style="margin:12px 0 6px">화자별 <span class="muted small">${{ regex: "발언 수: 화자 라벨을 직접 센 값", mixed: "발언 수: 일부만 화자 라벨로 측정, 나머지는 모델 추정", llm: "발언 수: 모델 추정" }[r.utterances_source] || ""}</span></h4>${table([{ key: "speaker", label: "화자" }, { key: "utterances", label: "발언", num: true }, { key: "main_points", label: "요지", render: v => v.map(esc).join("<br>") }], m.speakers)}
        ${usageHtml(r.usage)}`;
    });
  } },
]);

// #08 리서치 에이전트
DEMOS["08"] = el => {
  el.innerHTML = `<label>조사 주제</label><input type="text" id="t" maxlength="300" value="2026년 기업들의 AI 에이전트 도입 현황과 주요 과제">
    <div class="row"><div><label>최대 검색 횟수</label><input type="number" id="n" value="5" min="1" max="10"></div><div></div></div>
    <div class="actions"><button class="btn" id="go">조사 시작</button><span class="muted small">보통 1~3분 걸립니다 · 검색 횟수만큼 웹 검색 비용과 입력 토큰이 늘어납니다</span></div>
    <div class="cols research" style="margin-top:16px" id="wrap" hidden>
      <div><h4 style="margin:0 0 8px">진행 로그 <span class="muted small mono" id="clock"></span></h4><div class="timeline small" id="log"></div></div>
      <div><h4 style="margin:0 0 8px">보고서</h4><div class="box" id="rep"></div><div id="tail"></div><div id="u"></div></div></div>`;
  $("#go", el).onclick = async e => {
    const btn = e.target; btn.disabled = true;
    $("#wrap", el).hidden = false;
    const log = $("#log", el), rep = $("#rep", el), tail = $("#tail", el);
    log.innerHTML = ""; rep.innerHTML = loadingHtml("계획 수립 중…"); tail.innerHTML = ""; $("#u", el).innerHTML = "";
    const t0 = Date.now(), clock = setInterval(() => ($("#clock", el).textContent = `${Math.round((Date.now() - t0) / 1000)}초`), 1000);
    let text = "";
    const addLog = (cls, h, body = "") => log.insertAdjacentHTML("beforeend", `<div class="step ${cls}"><div class="h">${h}</div>${body}</div>`);
    try {
      await sse("/api/p08/research", { topic: $("#t", el).value, max_searches: num($("#n", el)) }, {
        delta: d => { text += d.text; rep.innerHTML = md(text); },
        tool: d => {
          // 도구 호출 전에 쓴 글은 보고서가 아니라 계획·중간 메모 → 로그로 옮기고 보고서 칸은 비운다
          if (text.trim()) {
            addLog("info", "📝 계획/메모", `<details><summary class="small">펼치기</summary><div class="small">${md(text)}</div></details>`);
            text = ""; rep.innerHTML = loadingHtml("조사 중…");
          }
          addLog("info", d.name === "web_search" ? "🔎 검색" : "📄 읽기", `<div class="muted">${esc((d.input && (d.input.query || d.input.url)) || "")}</div>`);
        },
        results: d => addLog("ok", `결과 ${d.items.length}건`, `<div class="muted">${d.items.slice(0, 3).map(i => esc(i.title)).join("<br>")}</div>`),
        fetched: d => addLog("ok", "본문 읽음", `<div class="muted">${esc(d.title && d.title !== d.url ? d.title : d.url || "")}</div>`),
        tool_error: d => addLog("bad", "도구 오류", esc(d.error)),
        status: d => addLog("info", esc(d.text)),
        done: d => {
          const sources = (d.sources || []).filter(s => /^https?:\/\//i.test(s.url || ""));
          addLog("ok", `완료 · 검색 결과 출처 ${sources.length}개`);
          // end_turn이 아니면 보고서가 중간에 끝났을 수 있음을 알린다
          const why = { pause_turn: "재개 한도(2회)에 도달", max_tokens: "토큰 한도에 도달", refusal: "모델이 응답을 거부" }[d.stop_reason];
          let html = d.stop_reason && d.stop_reason !== "end_turn" ? `<div class="note">${why || esc(d.stop_reason)} — 보고서가 불완전할 수 있습니다</div>` : "";
          // 보고서 '## 출처'의 URL을 이번 실행의 검색/열람 결과와 대조한 결과 (서버 계산)
          const ver = d.verified_urls || [], unv = d.unverified_urls || [];
          if (ver.length + unv.length) html += `<div class="small muted">출처 검증 ${ver.length}/${ver.length + unv.length}${d.web_searches != null ? ` · 웹 검색 ${d.web_searches}회 · 본문 열람 ${d.web_fetches || 0}회` : ""}</div>`;
          if (unv.length) html += `<details open><summary class="bad">이번 실행의 검색/열람 결과에 없는 URL ${unv.length}개</summary><ul class="small">${unv.map(u =>
            `<li class="mono">${esc(u)}</li>`).join("")}</ul></details>`;
          // 보고서의 [번호] 인용 목록이 아니라, 검색으로 확인한 URL 목록이다
          if (sources.length) html += `<details><summary>검색으로 확인한 출처 ${sources.length}개</summary><ul class="small">${sources.map(s =>
            `<li><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer nofollow">${esc(s.title || s.url)}</a></li>`).join("")}</ul></details>`;
          tail.innerHTML = html;
          if (!text) rep.innerHTML = '<span class="muted">(보고서 본문이 생성되지 않았습니다)</span>';
          $("#u", el).innerHTML = usageHtml(d.usage);
        },
      });
    } catch (err) {
      if (isAbort(err)) return; // 다른 화면으로 이동해 중단됨
      if (text) tail.insertAdjacentHTML("afterbegin", errHtml(err)); // 받은 부분 보고서는 지우지 않는다
      else rep.innerHTML = errHtml(err);
    } finally { clearInterval(clock); btn.disabled = false; }
  };
};

// #09 MCP
DEMOS["09"] = el => tabs(el, [
  { label: "서버 도구 (MCP 직접 호출)", render: async b => {
    b.innerHTML = loadingHtml("MCP 서버(stdio) 실행 및 initialize 중…");
    try {
      const r = await api("/api/p09/tools");
      b.innerHTML = `<div class="okbox">✓ MCP 핸드셰이크 성공 — 도구 ${r.tools.length} · 리소스 ${r.resources.length} · 프롬프트 ${r.prompts.length}</div>
        ${table([{ key: "name", label: "tool" }, { key: "description", label: "설명" }, { key: "input_schema", label: "입력 스키마", render: v => `<code class="small">${esc(JSON.stringify(v.properties))}</code>` }], r.tools)}
        <div class="row" style="margin-top:12px"><div><label>도구</label><select id="tn">${r.tools.map(t => `<option>${t.name}</option>`).join("")}</select></div>
          <div style="flex:2"><label>arguments (JSON)</label><input type="text" id="ta" value="{}"></div></div>
        <div class="actions"><button class="btn" id="call">call_tool</button></div><div class="out" id="o"></div>
        <h4 style="margin:18px 0 6px">Claude Desktop / Claude Code 연결</h4>
        <pre class="box">${esc(JSON.stringify(r.config, null, 2))}</pre>`;
      const presets = { list_notes: "{}", search_notes: '{"query": "레이트리밋"}', add_note: '{"title": "새 노트", "body": "MCP로 추가함", "tags": ["demo"]}', delete_note: '{"note_id": 4}' };
      const tn = $("#tn", b);
      // 첫 화면은 인자가 의미 있는 search_notes로 (선택한 도구와 arguments 예시가 항상 짝이 맞게)
      if (r.tools.some(t => t.name === "search_notes")) tn.value = "search_notes";
      tn.onchange = () => ($("#ta", b).value = presets[tn.value] || "{}");
      tn.onchange();
      $("#call", b).onclick = e => run(e.target, $("#o", b), async () => {
        let args;
        try { args = JSON.parse($("#ta", b).value || "{}"); } catch (err) { throw new Error("arguments가 올바른 JSON이 아닙니다: " + err.message); }
        const x = await api("/api/p09/call", { name: $("#tn", b).value, arguments: args });
        $("#o", b).innerHTML = `<pre class="box">${esc(x.text)}</pre>`;
      });
    } catch (e) { b.innerHTML = errHtml(e); }
  } },
  { label: "Claude + MCP 에이전트", render: b => {
    b.innerHTML = `<label>요청</label><input type="text" id="p" maxlength="1000" value="장애 관련 노트를 찾아서 핵심만 요약하고, '재발 방지 체크리스트'라는 노트를 incident 태그로 추가해줘">
      <div class="actions"><button class="btn" id="go">실행</button></div><div class="out" id="o"></div>`;
    $("#go", b).onclick = e => run(e.target, $("#o", b), async () => {
      const r = await api("/api/p09/agent", { prompt: $("#p", b).value });
      $("#o", b).innerHTML = `<div class="timeline">${r.trace.map(t => `<div class="step ${t.is_error ? "bad" : "ok"}"><div class="h">🔧 ${esc(t.tool)} <code class="small">${esc(JSON.stringify(t.input))}</code></div>
        <details><summary>결과</summary><pre class="box">${esc(t.output)}</pre></details></div>`).join("")}</div>
        <div class="box" style="margin-top:10px">${md(r.answer)}</div>${usageHtml(r.usage)}`;
    }, "Claude가 MCP 도구를 호출하는 중…");
  } },
]);

// #10 멀티 에이전트
DEMOS["10"] = el => {
  el.innerHTML = `<label>개발 과제</label><textarea id="t" maxlength="2000">한국 휴대폰 번호 문자열을 정규화하는 함수 normalize_phone(s)를 작성하라. 공백, 하이픈, 괄호, 국가번호(+82)가 섞여 들어올 수 있고, 결과는 010-1234-5678 형식이어야 한다. 유효하지 않은 번호는 ValueError를 발생시킨다.</textarea>
    <div class="row"><div><label>최대 리뷰 라운드</label><input type="number" id="r" value="2" min="0" max="3"></div><div></div></div>
    <div class="actions"><button class="btn" id="go">실행</button><span class="muted small" id="hint"></span></div>
    <div class="out" id="o"></div>`;
  // 호출 수 = 기획 1 + (개발+리뷰)×(라운드+1) + 단일 1 + 심사 2 (A/B 순서를 바꿔 두 번; 리뷰 승인 시 조기 종료)
  const hint = () => { const r = numVal($("#r", el)); $("#hint", el).textContent = `Claude 호출 최대 ${4 + 2 * (r + 1)}회 (기획 1 · 개발/리뷰 ${2 * (r + 1)} · 단일 1 · 심사 2)`; };
  $("#r", el).addEventListener("input", hint); hint();
  // 진행 표시는 전용 클래스로 찾는다 (.muted로 찾으면 앞 단계의 토큰·비용 라벨이 지워짐)
  const pending = t => `<div class="muted small pending"><span class="spinner"></span> ${esc(t)}</div>`;
  $("#go", el).onclick = async e => {
    const btn = e.target; btn.disabled = true;
    const o = $("#o", el);
    o.innerHTML = `<div class="timeline" id="tl"></div><div id="res"></div>`;
    const tl = $("#tl", o);
    const icon = a => a.startsWith("planner") ? "🧭 기획자" : a.startsWith("coder") ? `👩‍💻 개발자 ${a.split("#")[1]}차` : a.startsWith("reviewer") ? `🔍 리뷰어 ${a.split("#")[1]}차` : "🧑 단일 에이전트 (베이스라인)";
    tl.innerHTML = pending("기획자가 명세 작성 중…");
    const rounds = num($("#r", el)); hint();
    try {
      await sse("/api/p10/run", { task: $("#t", el).value, max_rounds: rounds }, {
        step: d => {
          $$(".pending", tl).forEach(x => x.remove());
          tl.insertAdjacentHTML("beforeend", `<div class="step ${d.agent.startsWith("reviewer") ? (d.text.startsWith("✅") ? "ok" : "bad") : "info"}">
            <div class="h">${icon(d.agent)} <span class="muted small mono">${d.usage.output_tokens} tok · $${d.usage.cost_usd.toFixed(4)}</span></div>
            <details ${d.agent.startsWith("reviewer") ? "open" : ""}><summary>내용</summary><pre class="box">${esc(d.text)}</pre></details></div>`);
          tl.insertAdjacentHTML("beforeend", pending("다음 단계 진행 중…"));
        },
        done: d => {
          $$(".pending", tl).forEach(x => x.remove());
          // 두 번 심사한 평균이라 x.5점이 나올 수 있다
          const s = x => Math.round((x.correctness + x.edge_cases + x.readability + x.tests) * 10) / 10;
          // 코드는 실행하지 않고 ast로만 본 정적 검사 결과
          const st = x => !x ? "-" : x.syntax_ok ? `구문 OK · assert ${x.asserts}개` : "✗ 구문 오류";
          const row = (k, m) => [k, s(m.score) + "/40", m.score.correctness, m.score.edge_cases, m.score.readability, m.score.tests, st(m.static), m.calls, m.seconds + "s", "$" + m.usage.cost_usd.toFixed(4)];
          const verdict = d.judge_consistent === false ? "무승부 (순서 교차 심사 불일치)" : { multi: "멀티 에이전트", single: "단일 에이전트", tie: "무승부" }[d.winner];
          $("#res", o).innerHTML = `<h4 style="margin:16px 0 6px">블라인드 교차 심사 결과 — 승자: <span class="good">${verdict}</span></h4>
            ${table(["방식", "총점", "정확성", "엣지케이스", "가독성", "테스트", "정적 검사", "호출 수", "시간", "비용"], [row("멀티 에이전트", d.multi), row("단일 에이전트", d.single)])}
            <p class="small muted">A/B 순서를 바꿔 ${d.judge_runs || 2}번 심사한 평균${d.judge_runs === 1 ? " (한쪽 심사만 성공)" : ""} · 두 판정이 일치할 때만 승자를 선언합니다. 한 번(n=1) 실행한 결과라 우연의 영향이 큽니다.</p>
            <div class="grid2" style="margin-top:10px"><div class="small"><b>멀티:</b> ${esc(d.multi.score.rationale)}</div><div class="small"><b>단일:</b> ${esc(d.single.score.rationale)}</div></div>
            <p class="small muted">비용 배수: 멀티 에이전트가 단일 대비 ${(d.multi.usage.cost_usd / Math.max(d.single.usage.cost_usd, 1e-9)).toFixed(1)}배. 품질 향상이 이 비용을 정당화하는지가 설계 판단의 핵심입니다.</p>
            ${usageHtml(d.judge_usage)}`;
        },
      });
    } catch (err) { if (!isAbort(err)) $("#res", o).innerHTML = errHtml(err); }
    finally { btn.disabled = false; $$(".pending", tl).forEach(x => x.remove()); }
  };
};

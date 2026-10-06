// Level 1–2 데모: #01 ~ #05

async function handbookText() {
  const d = await api("/api/p04/docs");
  return d.docs.map(x => x.text).join("\n\n");
}

// #01 요약/번역
DEMOS["01"] = el => {
  el.innerHTML = `
    <label>텍스트</label><textarea id="t" placeholder="요약하거나 번역할 텍스트를 붙여넣으세요"></textarea>
    <label>또는 파일 (.txt / .md / .pdf · 공개 데모라 입력 길이에 상한이 있습니다)</label><input type="file" id="f" accept=".txt,.md,.pdf">
    <div class="row">
      <div><label>모드</label><select id="mode"><option value="summarize">요약</option><option value="translate">번역</option></select></div>
      <div><label>언어</label><select id="lang"><option>한국어</option><option>English</option><option>日本語</option></select></div>
      <div><label>요약 길이</label><select id="len"><option value="short">짧게</option><option value="medium" selected>보통</option><option value="long">길게</option></select></div>
      <div><label>청크 크기(자)</label><input type="number" id="cs" value="1500" min="1000" max="8000" step="500"></div>
    </div>
    <div class="actions"><button class="btn" id="go">실행</button><button class="btn ghost" id="sample">샘플: 사내 규정 8개 문서</button>
      <span class="muted small" id="est"></span></div>
    <div class="out" id="o"></div>`;
  // 붙여넣은 텍스트 기준 대략적인 호출 수. 서버는 청크 크기를 1,000~8,000자로 맞추고 청크가 12개를 넘으면 크기를 키운다
  // (실제 청킹은 문단 경계 기준이라 조금 다를 수 있음)
  const MAX_CHUNKS = 12, MAX_TEXT = 120000;
  const estimate = () => {
    const len = $("#t", el).value.trim().length, cs = numVal($("#cs", el));
    if (!len || $("#f", el).files[0]) { $("#est", el).textContent = "청크 크기를 줄이면 Map-Reduce 동작을 볼 수 있지만, 청크마다 Claude를 한 번씩 호출합니다"; return; }
    if (len > MAX_TEXT) { $("#est", el).textContent = `${len.toLocaleString()}자 — 데모는 ${MAX_TEXT.toLocaleString()}자까지 처리합니다`; return; }
    const n = Math.min(MAX_CHUNKS, Math.max(1, Math.ceil(len / cs))), calls = n === 1 ? 1 : n + ($("#mode", el).value === "summarize" ? 1 : 0);
    $("#est", el).textContent = `${len.toLocaleString()}자 → 청크 약 ${n}개 · Claude 호출 약 ${calls}회`;
  };
  ["#t", "#cs"].forEach(s => $(s, el).addEventListener("input", estimate));
  ["#mode", "#f"].forEach(s => $(s, el).addEventListener("change", estimate));
  estimate();
  $("#sample", el).onclick = async () => {
    try { $("#t", el).value = await handbookText(); estimate(); }
    catch (err) { if (!isAbort(err)) $("#o", el).innerHTML = errHtml(err); }
  };
  $("#go", el).onclick = e => run(e.target, $("#o", el), async () => {
    const f = $("#f", el).files[0];
    if (f) { // 업로드 전에 걸러 대기 시간과 서버 메모리를 아낀다 (실제 토큰 상한은 서버가 검사)
      const pdf = /\.pdf$/i.test(f.name), lim = pdf ? 5 * 1024 * 1024 : 400 * 1024;
      if (f.size > lim) throw new Error(pdf ? "PDF는 5MB 이하만 올릴 수 있습니다 (공개 데모 입력 상한)." : `텍스트 파일은 400KB 이하만 올릴 수 있습니다 (공개 데모는 약 ${MAX_TEXT.toLocaleString()}자까지 처리).`);
    } else if ($("#t", el).value.trim().length > MAX_TEXT) {
      throw new Error(`데모에서는 ${MAX_TEXT.toLocaleString()}자까지 처리합니다. 문서를 나눠서 시도해 주세요.`);
    }
    const fd = new FormData();
    fd.append("text", $("#t", el).value); fd.append("mode", $("#mode", el).value);
    fd.append("lang", $("#lang", el).value); fd.append("length", $("#len", el).value);
    fd.append("chunk_chars", num($("#cs", el)));
    if (f) fd.append("file", f);
    const r = await api("/api/p01/run", fd);
    $("#o", el).innerHTML = `
      <div class="metrics">
        <div class="metric"><b>${esc(r.strategy)}</b><span>처리 전략</span></div>
        <div class="metric"><b>${r.chunks}</b><span>청크 수</span></div>
        <div class="metric"><b>${r.input_token_estimate.toLocaleString()}</b><span>입력 토큰 (count_tokens)</span></div>
        <div class="metric"><b>$${r.usage.cost_usd.toFixed(4)}</b><span>총 비용 · ${r.steps.length}회 호출</span></div>
      </div>
      ${r.truncated ? '<div class="note">일부 출력이 길이 제한(max_tokens)에 걸려 잘렸습니다. 문서를 나누거나 요약 길이를 줄여 보세요.</div>' : ""}
      <div class="box" style="margin-top:12px">${md(r.result)}</div>${usageHtml(r.usage)}`;
  }, "청크별로 처리 중…");
};

// #02 구조화 추출
const RECEIPT_SAMPLE = `[카페 루미나 강남점]
2026-09-28 14:32
아메리카노      2 x 4,500   9,000
카페라떼        1 x 5,000   5,000
치즈케이크      1 x 6,500   6,500
-----------------------------
합계                       20,500
부가세 포함
결제: 신한카드 (일시불)`;
const RESUME_SAMPLE = `홍길동 | gildong.hong@example.com | 010-0000-0000
백엔드 엔지니어 · 경력 6년

경력
- 루미나랩스 (2022.03 ~ 현재) 시니어 백엔드 엔지니어
  · LLM 기반 고객지원 자동화 시스템 설계, 응답 시간 40% 단축
  · Kafka 기반 이벤트 파이프라인 구축
- 데이터웨이브 (2019.01 ~ 2022.02) 백엔드 개발자
  · 결제 API 개발 및 MSA 전환

기술: Python, FastAPI, PostgreSQL, Kafka, AWS, Docker, LangGraph
학력: 한국대학교 컴퓨터공학과 학사 (2018)`;

DEMOS["02"] = el => {
  el.innerHTML = `
    <div class="row"><div><label>문서 종류</label><select id="k"><option value="receipt">영수증</option><option value="resume">이력서</option></select></div>
      <div><label>이미지 (선택)</label><input type="file" id="img" accept="image/*"></div></div>
    <label>텍스트</label><textarea id="t" class="code">${esc(RECEIPT_SAMPLE)}</textarea>
    <div class="muted small">이미지를 올리면 샘플 텍스트는 비워집니다. 남아 있는 텍스트는 이미지와 함께 전송됩니다.</div>
    <div class="actions"><button class="btn" id="go">추출</button>
      <button class="btn ghost" id="bad">오류가 있는 영수증으로 테스트</button></div>
    <div class="out" id="o"></div>`;
  // 샘플 텍스트가 업로드한 이미지와 함께 가면 모델이 두 문서를 섞어 추출하므로 비운다
  const BAD_RECEIPT = RECEIPT_SAMPLE.replace("20,500", "21,000");
  const isSample = v => [RECEIPT_SAMPLE, RESUME_SAMPLE, BAD_RECEIPT].includes(v);
  $("#img", el).onchange = () => { const t = $("#t", el); if ($("#img", el).files[0] && isSample(t.value)) t.value = ""; };
  $("#k", el).onchange = () => { if ($("#img", el).files[0]) return; $("#t", el).value = $("#k", el).value === "receipt" ? RECEIPT_SAMPLE : RESUME_SAMPLE; };
  $("#bad", el).onclick = () => { $("#img", el).value = ""; $("#k", el).value = "receipt"; $("#t", el).value = BAD_RECEIPT; };
  $("#go", el).onclick = e => run(e.target, $("#o", el), async () => {
    const img = $("#img", el).files[0];
    const fd = new FormData();
    fd.append("kind", $("#k", el).value); fd.append("text", $("#t", el).value);
    if (img) fd.append("image", await prepImage(img));
    const r = await api("/api/p02/extract", fd);
    const last = r.attempts[r.attempts.length - 1];
    $("#o", el).innerHTML = `
      ${r.valid ? `<div class="okbox">✓ 스키마와 의미 검증을 모두 통과했습니다 (시도 ${r.attempts.length}회)</div>`
                : `<div class="note">원문 자체에 불일치가 있습니다. ${r.converged ? `재검토해도 같은 값이 나와 원문 자체 불일치로 판단해 조기 종료했습니다 (시도 ${r.attempts.length}회). 남은 오류` : `${r.attempts.length}회 재검토 후에도 남은 오류`}: ${last ? last.errors.map(esc).join(" / ") : "-"}</div>`}
      <div class="timeline" style="margin:12px 0">${r.attempts.map(a => `<div class="step ${a.errors.length ? "bad" : "ok"}"><div class="h">시도 ${a.attempt}</div>
        <div class="small muted">${a.errors.length ? a.errors.map(esc).join("<br>") : "검증 통과"}</div></div>`).join("")}</div>
      <div class="grid2"><div><div class="small muted">추출 결과</div><pre class="box">${esc(JSON.stringify(r.data, null, 2))}</pre></div>
        <div><div class="small muted">JSON Schema (Pydantic)</div><pre class="box" style="max-height:360px">${esc(JSON.stringify(r.schema, null, 2))}</pre></div></div>
      ${usageHtml(r.usage)}`;
  });
};

// #03 스트리밍 챗봇
DEMOS["03"] = el => {
  const state = { messages: [], summary: "" };
  let busy = false; // 버튼뿐 아니라 Enter 경로도 막아 스트림이 겹치지 않게 한다
  el.innerHTML = `
    <div class="chat box" id="c"><div class="msg system">대화가 4,000자를 넘으면 오래된 턴이 요약 메모리로 압축됩니다. 자기소개를 하고 길게 대화한 뒤, 이름을 기억하는지 물어보세요.</div></div>
    <div class="row" style="margin-top:10px"><div style="flex:5"><input type="text" id="in" maxlength="4000" placeholder="메시지를 입력하세요"></div><div style="flex:0"><button class="btn" id="send">전송</button></div></div>
    <details><summary>현재 요약 메모리</summary><pre class="box" id="sum">(없음)</pre></details>
    <div id="u"></div>`;
  const chat = $("#c", el);
  const add = (role, text) => { const d = document.createElement("div"); d.className = `msg ${role}`; d.textContent = text; chat.appendChild(d); chat.scrollTop = chat.scrollHeight; return d; };
  const send = async () => {
    if (busy) return;
    const text = $("#in", el).value.trim();
    if (!text) return;
    busy = true; // 빈 입력 return 뒤에 세워야 busy가 영영 true로 남지 않는다
    $("#in", el).value = "";
    add("user", text);
    state.messages.push({ role: "user", content: text });
    const bubble = add("assistant", "");
    bubble.innerHTML = '<span class="spinner"></span>';
    let acc = "", stop = null;
    $("#send", el).disabled = true;
    try {
      await sse("/api/p03/chat", { messages: state.messages, summary: state.summary }, {
        compressed: d => {
          state.summary = d.summary; state.messages = d.messages; // d.messages는 이번 user 메시지로 끝난다
          $("#sum", el).textContent = d.summary;
          const s = add("system", `🗜 오래된 메시지 ${d.dropped}개를 요약 메모리로 압축했습니다 ($${d.usage.cost_usd.toFixed(4)})`);
          chat.insertBefore(s, bubble);
        },
        delta: d => { acc += d.text; bubble.textContent = acc; chat.scrollTop = chat.scrollHeight; },
        done: d => { stop = d.stop_reason; $("#u", el).innerHTML = usageHtml(d.usage); },
      });
      if (stop === "refusal" || !acc.trim()) {
        // 빈/거절된 assistant 턴을 기록하면 이후 모든 요청이 400이 되므로 이번 user 턴째 버린다
        state.messages.pop();
        bubble.className = "msg system";
        bubble.textContent = stop === "refusal"
          ? "⚠ 모델이 이 요청에 대한 답변을 거절했습니다. 다른 질문을 해 주세요."
          : "⚠ 빈 응답을 받았습니다. 다시 시도해 주세요.";
      } else {
        state.messages.push({ role: "assistant", content: acc });
        if (stop === "max_tokens") add("system", "(응답이 길이 제한으로 잘렸습니다)");
      }
    } catch (e) {
      if (isAbort(e)) return; // 다른 화면으로 이동함
      bubble.className = "msg system"; bubble.textContent = "⚠ " + e.message; state.messages.pop();
    } finally { busy = false; $("#send", el).disabled = false; }
  };
  $("#send", el).onclick = send;
  // keyCode 229: 일부 브라우저에서 한글 IME 조합 중 Enter (MDN 권장 가드)
  $("#in", el).onkeydown = e => { if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) send(); };
};

// #04 RAG
DEMOS["04"] = el => {
  tabs(el, [
    { label: "질의응답", render: async b => {
      const d = await api("/api/p04/docs");
      b.innerHTML = `
        <label>질문</label><input type="text" id="q" maxlength="500" value="남은 연차 내년에 쓸 수 있어? 언제까지?">
        <div class="row"><div><label>청킹</label><select id="ch">${d.chunkers.map(c => `<option ${c === "heading-contextual" ? "selected" : ""}>${c}</option>`).join("")}</select></div>
          <div><label>검색기</label><select id="rt">${d.retrievers.map(c => `<option ${c === "hybrid-rrf" ? "selected" : ""}>${c}</option>`).join("")}</select></div>
          <div><label>top-k</label><input type="number" id="k" value="4" min="1" max="8"></div></div>
        <div class="actions"><button class="btn" id="ask">답변 생성</button><button class="btn ghost" id="srch">검색만 (LLM 없이)</button></div>
        <div class="out" id="o"></div>`;
      const body = () => ({ question: $("#q", b).value, chunker: $("#ch", b).value, retriever: $("#rt", b).value, k: num($("#k", b)) });
      const showRetrieved = list => table([{ key: "title", label: "청크" }, { key: "text", label: "내용", render: v => `<span class="small">${esc(v)}</span>` }, { key: "score", label: "점수", num: true }], list);
      $("#srch", b).onclick = e => run(e.target, $("#o", b), async () => {
        const r = await api("/api/p04/search", body());
        $("#o", b).innerHTML = r.results.length ? showRetrieved(r.results) : '<div class="note">질문과 겹치는 청크가 없습니다.</div>';
      });
      $("#ask", b).onclick = e => run(e.target, $("#o", b), async () => {
        const r = await api("/api/p04/ask", body());
        const ans = r.answer.map(p => esc(p.text) + p.citations.map(c => `<cite title="${esc(c.title + ": " + c.text)}">${c.doc_index + 1}</cite>`).join("")).join("");
        const got = r.retrieved || [];
        $("#o", b).innerHTML = `<div class="box" style="white-space:pre-wrap">${ans}</div>${usageHtml(r.usage)}
          ${r.abstained ? '<div class="small muted">검색 단계에서 차단 · LLM 미호출 · $0</div>' : ""}
          ${got.length ? `<h4 style="margin:16px 0 6px">검색된 청크 (번호 = 인용 번호)</h4>${showRetrieved(got.map((x, i) => ({ ...x, title: `${i + 1}. ${x.title}` })))}` : ""}`;
      });
    } },
    { label: "검색 품질 평가 (Recall@k)", render: async b => {
      b.innerHTML = loadingHtml("25개 평가 질문으로 9개 조합 측정 중…");
      const r = await api("/api/p04/eval");
      const pct = v => `${(v * 100).toFixed(0)}%`;
      // JSON으로 오면 r.best와 r.rows[i]는 별개 객체라 ===로 비교할 수 없다 → 행의 best 플래그 또는 키로 비교
      const isBest = x => x.best === true || (!!r.best && x.chunker === r.best.chunker && x.retriever === r.best.retriever);
      const best = r.rows.find(isBest);
      const cols = [{ key: "chunker", label: "청킹" }, { key: "retriever", label: "검색기" }, { key: "chunks", label: "청크 수", num: true },
        { key: "recall@1", label: "R@1", num: true, render: pct }, { key: "recall@3", label: "R@3", num: true, render: pct },
        { key: "recall@5", label: "R@5", num: true, render: pct }, { key: "mrr", label: "MRR", num: true }];
      if (r.rows.length && r.rows[0].reachable != null) cols.push({ key: "reachable", label: "도달 가능", num: true, render: v => `${v}/${r.cases}` });
      b.innerHTML = `<p class="small muted">평가 질문 ${r.cases}개(구어체, 원문 표현과 다르게 작성)에 대해 정답 구절이 top-k 청크 안에 있는지를 측정했습니다. LLM은 호출하지 않습니다.</p>
        ${table(cols, r.rows, { rowClass: x => (isBest(x) ? "best" : "") })}
        <h4 style="margin:16px 0 6px">Recall@3 비교</h4>
        ${bars(r.rows.map(x => ({ label: `${x.chunker} · ${x.retriever}${isBest(x) ? " ★" : ""}`, value: x["recall@3"] })))}
        <p class="small muted">초록색 행(★)이 Recall@3 기준 최고 조합입니다(동점이면 MRR). 데이터나 질문이 바뀌면 순위도 바뀌므로, 측정하고 나서 고르는 과정 자체가 핵심입니다.</p>
        <p class="small muted">${r.note ? esc(r.note) : `질문 1개 = ${(100 / r.cases).toFixed(0)}%p라 작은 차이는 문항 한두 개 차이입니다.`}</p>
        ${best && best.misses && best.misses.length ? `<details><summary>최고 조합이 top-5 안에서도 찾지 못한 질문 ${best.misses.length}개</summary><ul class="small">${best.misses.map(q => `<li>${esc(q)}</li>`).join("")}</ul></details>` : ""}`;
    } },
    { label: "원본 문서", render: async b => {
      const d = await api("/api/p04/docs");
      b.innerHTML = d.docs.map(x => `<details><summary>${esc(x.name)}</summary><div class="box">${md(x.text)}</div></details>`).join("");
    } },
  ]);
};

// #05 Text-to-SQL
DEMOS["05"] = el => {
  const examples = ["월별 매출 추이를 보여줘", "카테고리별 매출 순위", "골드 등급 고객의 도시별 평균 주문 금액", "반품률이 가장 높은 상품 5개", "2025년 11~12월과 다른 달의 일평균 주문 수 비교"];
  el.innerHTML = `
    <label>질문</label><input type="text" id="q" maxlength="500" value="${examples[0]}">
    <div class="actions">${examples.map(x => `<button class="btn ghost small ex">${esc(x)}</button>`).join("")}</div>
    <div class="actions"><button class="btn" id="go">분석</button><button class="btn ghost" id="schema">스키마 보기</button>
      <button class="btn ghost" id="attack">쓰기 쿼리 차단 테스트</button></div>
    <div class="out" id="o"></div>`;
  $$(".ex", el).forEach(b => (b.onclick = () => ($("#q", el).value = b.textContent)));
  $("#schema", el).onclick = e => run(e.target, $("#o", el), async () => {
    const r = await api("/api/p05/schema");
    $("#o", el).innerHTML = `<pre class="box">${esc(r.ddl)}</pre>` + Object.entries(r.tables).map(([t, d]) =>
      `<h4 style="margin:12px 0 4px">${t} <span class="muted small">${d.count.toLocaleString()}행</span></h4>${table(d.columns, d.rows)}`).join("");
  });
  $("#attack", el).onclick = e => run(e.target, $("#o", el), async () => {
    const qs = ["DELETE FROM orders", "DROP TABLE customers", "UPDATE products SET price = 0", "ATTACH DATABASE 'x.db' AS x"];
    const rs = await Promise.all(qs.map(q => api("/api/p05/sql", { question: q })));
    $("#o", el).innerHTML = table(["쿼리", "결과"], qs.map((q, i) => [q, rs[i].ok ? "⚠ 실행됨" : "🛡 " + rs[i].error]));
  });
  const traceHtml = trace => (trace && trace.length) ? `<div class="timeline">${trace.map((t, i) => `<div class="step ${t.ok ? "ok" : "bad"}"><div class="h">#${i + 1} ${esc(t.tool || "run_sql")} → ${t.ok ? `${t.rows}행` : "오류"}</div>
    <pre class="box">${esc(t.query)}</pre>${t.error ? `<div class="small bad">${esc(t.error)}</div>` : ""}</div>`).join("")}</div>` : "";
  $("#go", el).onclick = e => run(e.target, $("#o", el), async () => {
    const r = await api("/api/p05/ask", { question: $("#q", el).value });
    if (r.error) { // 반복 상한 도달: 이미 쓴 호출의 기록과 비용은 보여준다
      $("#o", el).innerHTML = errHtml(r.error) + `<h4 style="margin:14px 0 6px">에이전트 실행 기록</h4>` + traceHtml(r.trace) + usageHtml(r.usage);
      return;
    }
    const { final, data } = r;
    let viz = "";
    if (data.ok) { // 최종 SQL 재실행이 실패하면 {ok:false, error}만 온다
      const xi = data.columns.indexOf(final.x), yi = data.columns.indexOf(final.y);
      if (final.chart !== "table" && xi >= 0 && yi >= 0 && data.rows.length)
        viz = chart(final.chart, data.rows.map(x => x[xi]), data.rows.map(x => +x[yi] || 0));
    }
    $("#o", el).innerHTML = `
      <div class="box">${esc(final.answer)}</div>
      ${viz ? `<div class="box">${viz}</div>` : ""}
      <h4 style="margin:14px 0 6px">에이전트 실행 기록 <span class="muted small">자기 수정 ${r.self_corrections}회</span></h4>
      ${traceHtml(r.trace)}
      <h4 style="margin:14px 0 6px">최종 SQL</h4><pre class="box">${esc(final.sql)}</pre>
      ${data.ok ? `<div style="margin-top:10px">${table(data.columns, data.rows.slice(0, 50))}</div>${data.rows.length > 50 || data.truncated ? `<div class="muted small">(${data.truncated ? "200행에서 잘림 · " : ""}상위 50행만 표시)</div>` : ""}`
                : `<div class="err" style="margin-top:10px">최종 SQL 실행 오류: ${esc(data.error)}</div>`}
      ${usageHtml(r.usage)}`;
  }, "SQL 생성 → 실행 → 검증 중…");
};

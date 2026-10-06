// Level 1–2 데모: #01 ~ #05

async function handbookText() {
  const d = await api("/api/p04/docs");
  return d.docs.map(x => x.text).join("\n\n");
}

// #01 요약/번역
DEMOS["01"] = el => {
  el.innerHTML = `
    <label>텍스트</label><textarea id="t" placeholder="요약하거나 번역할 텍스트를 붙여넣으세요"></textarea>
    <label>또는 파일 (.txt / .md / .pdf)</label><input type="file" id="f" accept=".txt,.md,.pdf">
    <div class="row">
      <div><label>모드</label><select id="mode"><option value="summarize">요약</option><option value="translate">번역</option></select></div>
      <div><label>언어</label><select id="lang"><option>한국어</option><option>English</option><option>日本語</option></select></div>
      <div><label>요약 길이</label><select id="len"><option value="short">짧게</option><option value="medium" selected>보통</option><option value="long">길게</option></select></div>
      <div><label>청크 크기(자)</label><input type="number" id="cs" value="1500" min="500" step="500"></div>
    </div>
    <div class="actions"><button class="btn" id="go">실행</button><button class="btn ghost" id="sample">샘플: 사내 규정 8개 문서</button>
      <span class="muted small">청크 크기를 줄이면 Map-Reduce 동작을 확인할 수 있습니다</span></div>
    <div class="out" id="o"></div>`;
  $("#sample", el).onclick = async () => ($("#t", el).value = await handbookText());
  $("#go", el).onclick = e => run(e.target, $("#o", el), async () => {
    const fd = new FormData();
    fd.append("text", $("#t", el).value); fd.append("mode", $("#mode", el).value);
    fd.append("lang", $("#lang", el).value); fd.append("length", $("#len", el).value);
    fd.append("chunk_chars", $("#cs", el).value);
    if ($("#f", el).files[0]) fd.append("file", $("#f", el).files[0]);
    const r = await api("/api/p01/run", fd);
    $("#o", el).innerHTML = `
      <div class="metrics">
        <div class="metric"><b>${r.strategy}</b><span>처리 전략</span></div>
        <div class="metric"><b>${r.chunks}</b><span>청크 수</span></div>
        <div class="metric"><b>${r.input_token_estimate.toLocaleString()}</b><span>입력 토큰 (count_tokens)</span></div>
        <div class="metric"><b>$${r.usage.cost_usd.toFixed(4)}</b><span>총 비용 · ${r.steps.length}회 호출</span></div>
      </div>
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
    <div class="actions"><button class="btn" id="go">추출</button>
      <button class="btn ghost" id="bad">오류가 있는 영수증으로 테스트</button></div>
    <div class="out" id="o"></div>`;
  $("#k", el).onchange = () => ($("#t", el).value = $("#k", el).value === "receipt" ? RECEIPT_SAMPLE : RESUME_SAMPLE);
  $("#bad", el).onclick = () => { $("#k", el).value = "receipt"; $("#t", el).value = RECEIPT_SAMPLE.replace("20,500", "21,000"); };
  $("#go", el).onclick = e => run(e.target, $("#o", el), async () => {
    const fd = new FormData();
    fd.append("kind", $("#k", el).value); fd.append("text", $("#t", el).value);
    if ($("#img", el).files[0]) fd.append("image", $("#img", el).files[0]);
    const r = await api("/api/p02/extract", fd);
    $("#o", el).innerHTML = `
      ${r.valid ? `<div class="okbox">✓ 스키마와 의미 검증을 모두 통과했습니다 (시도 ${r.attempts.length}회)</div>`
                : `<div class="note">원문 자체에 불일치가 있습니다. ${r.attempts.length}회 재검토 후에도 남은 오류: ${r.attempts.at(-1).errors.map(esc).join(" / ")}</div>`}
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
  el.innerHTML = `
    <div class="chat box" id="c"><div class="msg system">대화가 4,000자를 넘으면 오래된 턴이 요약 메모리로 압축됩니다. 자기소개를 하고 길게 대화한 뒤, 이름을 기억하는지 물어보세요.</div></div>
    <div class="row" style="margin-top:10px"><div style="flex:5"><input type="text" id="in" placeholder="메시지를 입력하세요"></div><div style="flex:0"><button class="btn" id="send">전송</button></div></div>
    <details><summary>현재 요약 메모리</summary><pre class="box" id="sum">(없음)</pre></details>
    <div id="u"></div>`;
  const chat = $("#c", el);
  const add = (role, text) => { const d = document.createElement("div"); d.className = `msg ${role}`; d.textContent = text; chat.appendChild(d); chat.scrollTop = chat.scrollHeight; return d; };
  const send = async () => {
    const text = $("#in", el).value.trim();
    if (!text) return;
    $("#in", el).value = "";
    add("user", text);
    state.messages.push({ role: "user", content: text });
    const bubble = add("assistant", "");
    bubble.innerHTML = '<span class="spinner"></span>';
    let acc = "";
    $("#send", el).disabled = true;
    try {
      await sse("/api/p03/chat", { messages: state.messages, summary: state.summary }, {
        compressed: d => {
          state.summary = d.summary; state.messages = d.messages;
          $("#sum", el).textContent = d.summary;
          const s = add("system", `🗜 오래된 메시지 ${d.dropped}개를 요약 메모리로 압축했습니다 ($${d.usage.cost_usd.toFixed(4)})`);
          chat.insertBefore(s, bubble);
        },
        delta: d => { acc += d.text; bubble.textContent = acc; chat.scrollTop = chat.scrollHeight; },
        done: d => { $("#u", el).innerHTML = usageHtml(d.usage); },
      });
      state.messages.push({ role: "assistant", content: acc });
    } catch (e) { bubble.className = "msg system"; bubble.textContent = "⚠ " + e.message; state.messages.pop(); }
    finally { $("#send", el).disabled = false; }
  };
  $("#send", el).onclick = send;
  $("#in", el).onkeydown = e => { if (e.key === "Enter" && !e.isComposing) send(); };
};

// #04 RAG
DEMOS["04"] = el => {
  tabs(el, [
    { label: "질의응답", render: async b => {
      const d = await api("/api/p04/docs");
      b.innerHTML = `
        <label>질문</label><input type="text" id="q" value="남은 연차 내년에 쓸 수 있어? 언제까지?">
        <div class="row"><div><label>청킹</label><select id="ch">${d.chunkers.map(c => `<option ${c === "heading-contextual" ? "selected" : ""}>${c}</option>`).join("")}</select></div>
          <div><label>검색기</label><select id="rt">${d.retrievers.map(c => `<option ${c === "hybrid-rrf" ? "selected" : ""}>${c}</option>`).join("")}</select></div>
          <div><label>top-k</label><input type="number" id="k" value="4" min="1" max="8"></div></div>
        <div class="actions"><button class="btn" id="ask">답변 생성</button><button class="btn ghost" id="srch">검색만 (LLM 없이)</button></div>
        <div class="out" id="o"></div>`;
      const body = () => ({ question: $("#q", b).value, chunker: $("#ch", b).value, retriever: $("#rt", b).value, k: +$("#k", b).value });
      const showRetrieved = list => table([{ key: "title", label: "청크" }, { key: "text", label: "내용", render: v => `<span class="small">${esc(v)}</span>` }, { key: "score", label: "점수", num: true }], list);
      $("#srch", b).onclick = e => run(e.target, $("#o", b), async () => { const r = await api("/api/p04/search", body()); $("#o", b).innerHTML = showRetrieved(r.results); });
      $("#ask", b).onclick = e => run(e.target, $("#o", b), async () => {
        const r = await api("/api/p04/ask", body());
        const ans = r.answer.map(p => esc(p.text) + p.citations.map(c => `<cite title="${esc(c.title + ": " + c.text)}">${c.doc_index + 1}</cite>`).join("")).join("");
        $("#o", b).innerHTML = `<div class="box" style="white-space:pre-wrap">${ans}</div>${usageHtml(r.usage)}
          <h4 style="margin:16px 0 6px">검색된 청크 (번호 = 인용 번호)</h4>${showRetrieved(r.retrieved.map((x, i) => ({ ...x, title: `${i + 1}. ${x.title}` })))}`;
      });
    } },
    { label: "검색 품질 평가 (Recall@k)", render: async b => {
      b.innerHTML = loadingHtml("25개 평가 질문으로 9개 조합 측정 중…");
      const r = await api("/api/p04/eval");
      const pct = v => `${(v * 100).toFixed(0)}%`;
      b.innerHTML = `<p class="small muted">평가 질문 ${r.cases}개(구어체, 원문 표현과 다르게 작성)에 대해 정답 구절이 top-k 청크 안에 있는지를 측정했습니다. LLM은 호출하지 않습니다.</p>
        ${table([{ key: "chunker", label: "청킹" }, { key: "retriever", label: "검색기" }, { key: "chunks", label: "청크 수", num: true },
          { key: "recall@1", label: "R@1", num: true, render: pct }, { key: "recall@3", label: "R@3", num: true, render: pct },
          { key: "recall@5", label: "R@5", num: true, render: pct }, { key: "mrr", label: "MRR", num: true }], r.rows,
          { rowClass: x => (x === r.best ? "best" : "") })}
        <h4 style="margin:16px 0 6px">Recall@3 비교</h4>
        ${bars(r.rows.map(x => ({ label: `${x.chunker} · ${x.retriever}`, value: x["recall@3"] })))}
        <p class="small muted">초록색 행이 Recall@3 기준 최고 조합입니다. 데이터나 질문이 바뀌면 순위도 바뀌므로, 측정하고 나서 고르는 과정 자체가 핵심입니다.</p>`;
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
    <label>질문</label><input type="text" id="q" value="${examples[0]}">
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
  $("#go", el).onclick = e => run(e.target, $("#o", el), async () => {
    const r = await api("/api/p05/ask", { question: $("#q", el).value });
    if (r.error) throw new Error(r.error);
    const { final, data } = r;
    let viz = "";
    const xi = data.columns.indexOf(final.x), yi = data.columns.indexOf(final.y);
    if (final.chart !== "table" && xi >= 0 && yi >= 0 && data.rows.length)
      viz = chart(final.chart, data.rows.map(x => x[xi]), data.rows.map(x => +x[yi] || 0));
    $("#o", el).innerHTML = `
      <div class="box">${esc(final.answer)}</div>
      ${viz ? `<div class="box">${viz}</div>` : ""}
      <h4 style="margin:14px 0 6px">에이전트 실행 기록 <span class="muted small">자기 수정 ${r.self_corrections}회</span></h4>
      <div class="timeline">${r.trace.map((t, i) => `<div class="step ${t.ok ? "ok" : "bad"}"><div class="h">#${i + 1} run_sql → ${t.ok ? `${t.rows}행` : "오류"}</div>
        <pre class="box">${esc(t.query)}</pre>${t.error ? `<div class="small bad">${esc(t.error)}</div>` : ""}</div>`).join("")}</div>
      <h4 style="margin:14px 0 6px">최종 SQL</h4><pre class="box">${esc(final.sql)}</pre>
      <div style="margin-top:10px">${table(data.columns, data.rows.slice(0, 50))}</div>${usageHtml(r.usage)}`;
  }, "SQL 생성 → 실행 → 검증 중…");
};

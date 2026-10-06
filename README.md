# AI Engineering Portfolio

LLM API 기초부터 RAG, 에이전트, 평가, 파인튜닝, 프로덕션 게이트웨이, 보안까지 **15개 프로젝트를 한 사이트에서 직접 실행**해 볼 수 있는 포트폴리오입니다. (#12 LoRA 학습·평가와 #13 로컬 서빙 벤치마크는 GPU/Ollama가 있는 로컬 환경에서 실행합니다.)

**▶ 라이브 데모: https://ai-portfolio-gwtu.onrender.com** (무료 플랜이라 첫 접속 시 깨어나는 데 30초~1분 걸릴 수 있습니다)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Uadj/ai-portfolio)

## 빠른 시작

```bash
pip install -r requirements.txt
cp .env.example .env        # ANTHROPIC_API_KEY 입력
python server.py            # → http://localhost:8000
```

API 키가 없어도 사이트는 열립니다. 아래 ✅ 표시가 있는 기능은 LLM 없이 동작합니다.

## 프로젝트

| # | 프로젝트 | 핵심 기술 | 키 없이 동작 |
|---|---|---|---|
| 01 | 문서 요약·번역 | Map-Reduce 청킹, count_tokens, PDF 네이티브 입력 | |
| 02 | 구조화 데이터 추출 | Structured Outputs(Pydantic), 의미 검증 → 피드백 재시도, Vision | |
| 03 | 스트리밍 챗봇 | SSE, 오래된 턴 자동 요약 압축 | |
| 04 | 사내 문서 RAG | 청킹 3종 × 검색기 3종(BM25 / char TF-IDF / RRF), Recall@k·MRR, Citations | ✅ 평가·검색 |
| 05 | Text-to-SQL | Tool use 루프, 자기 수정, sqlite authorizer 읽기 전용, SVG 차트 | ✅ 스키마·쓰기 차단 |
| 06 | 코드 리뷰 봇 | diff 파서, 구조화 리뷰, 무효 라인 필터, GitHub 웹훅(HMAC) | |
| 07 | 멀티모달 | 이미지 구조화 분석, 회의록·액션아이템 | |
| 08 | 리서치 에이전트 | web_search / web_fetch 서버 툴, 진행 상황 SSE, pause_turn 재개 | |
| 09 | MCP 서버 | mcp 2.x MCPServer(tools/resources/prompts), stdio 클라이언트, Claude 브릿지 | ✅ 도구 호출 |
| 10 | 멀티 에이전트 | Planner → Coder ↔ Reviewer, 단일 에이전트와 블라인드 A/B 비교 | |
| 11 | Eval 프레임워크 | 결정적 채점 + LLM 심사, 회귀 탐지, CI 게이트(GitHub Actions) | |
| 12 | LoRA 파인튜닝 | 교사 모델 데이터 합성 + 환각 필터, TRL/PEFT 학습, 전후 비교 | |
| 13 | 로컬 서빙 벤치마크 | Ollama, TTFT, 처리량, p50/p95 | ✅ (로컬 Ollama 필요, 공개 사이트에서는 실행 안 됨) |
| 14 | LLM 게이트웨이 | 라우팅, 응답 캐시, 프롬프트 캐싱, 토큰 버킷, 폴백 체인 | |
| 15 | 가드레일 | PII 마스킹(Luhn), 룰+LLM 인젝션 탐지, 카나리 토큰, 방어율 측정 | ✅ 룰 벤치마크 |

## 구조

```
server.py               FastAPI 앱 (projects/*.router 자동 등록)
core/llm.py             Claude 래퍼: 모델, effort, refusal fallback, 사용량·비용 계산
core/sse.py             SSE 헬퍼
core/net.py             레이트 리밋용 클라이언트 IP 판별 (위조 가능한 X-Forwarded-For 대신 엣지 헤더)
projects/p01~p15_*.py   프로젝트별 API
evals/                  #11 Eval 엔진 + 프롬프트 v1/v2
mcp_server/             #09 MCP 서버
p12_finetune/           #12 데이터 합성 / 학습 / 평가 스크립트
scripts/run_eval.py     CI용 Eval 실행기 (회귀 시 exit 1)
data/                   가상 회사 문서, 평가셋, 공격셋, SQLite DB(첫 실행 시 생성)
static/                 프론트엔드 (빌드 없는 바닐라 JS)
tests/                  오프라인 회귀 테스트 — 실제 Claude API를 부르지 않음 (python tests/test_core.py 등, pytest도 가능)
```

## 배포 (Render)

위 버튼을 누르면 `render.yaml` 블루프린트로 웹 서비스가 만들어집니다. 배포 화면에서 `ANTHROPIC_API_KEY`만 입력하면 됩니다.

공개 사이트용 비용 보호 장치 (`render.yaml`에서 `DAILY_BUDGET_USD=1`, `LLM_REQUESTS_PER_HOUR=30`으로 설정합니다. **코드 기본값은 0(무제한)이므로 다른 환경에 공개 배포할 때는 반드시 직접 지정하세요.**)
- `DAILY_BUDGET_USD`: 누적 Claude 사용액이 이 금액을 넘으면 LLM 기능을 멈춥니다(날짜는 서버 시간 기준, Render는 UTC). LLM이 필요 없는 측정 기능은 계속 동작합니다. 사용액 카운터는 프로세스 메모리에 있어 인스턴스가 잠들거나 재시작·재배포되면 0으로 돌아가므로, 엄밀한 하루 상한이 아닙니다. 확실한 상한은 Anthropic 콘솔의 사용 한도(spend limit)로 거세요.
- `LLM_REQUESTS_PER_HOUR`: IP당 시간당 POST 요청 수를 제한합니다(역시 메모리 기준). 이름과 달리 LLM을 부르지 않는 POST(#04 검색, #05 SQL, #09 도구 호출, #14 버스트 등)도 함께 세며, GitHub 웹훅만 제외합니다. IP는 Cloudflare가 넣는 `CF-Connecting-IP`로 판별하므로, 배포 후 서버 로그의 `rate-limit 프록시 헤더` 줄에 이 헤더가 보이는지 한 번 확인하세요.
- 무료 플랜은 15분 동안 요청이 없으면 잠들고, 다시 깨어날 때 메모리 상태(사용액 카운터, 요청 제한 기록, #14 게이트웨이 통계·응답 캐시)와 디스크 상태(#09 MCP 노트, #11 Eval 기록)가 모두 초기화됩니다.
- 사이트 상단 배지에 오늘 남은 데모 예산이 표시되고, 호출이 많은 데모는 실행 버튼 옆에 Claude 호출 횟수를 안내합니다.

## 설정

| 변수 | 용도 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API 키 (필수) |
| `CLAUDE_MODEL` | 기본 모델 (기본값 `claude-opus-5-5`) |
| `CLAUDE_FAST_MODEL` | 게이트웨이 경량 라우팅 모델 (기본값 `claude-haiku-4-5`) |
| `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET` | #06 PR 자동 리뷰 |
| `DAILY_BUDGET_USD`, `LLM_REQUESTS_PER_HOUR` | 공개 배포용 비용·남용 방지 (코드 기본값 0 = 무제한, render.yaml은 1 / 30) |
| `MAX_BODY_MB` | POST 요청 본문 상한 (기본값 12, 0 = 무제한, Content-Length 기준 1차 방어선. 이미지·diff 등은 각 라우트가 더 작은 상한을 따로 검사) |
| `CLIENT_IP_HEADER` | 레이트 리밋 키로 믿을 엣지 헤더 (기본값 `cf-connecting-ip`). Cloudflare 뒤가 아니면 빈 값으로 두세요 (그대로 두면 방문자가 위조 가능) |
| `TRUSTED_PROXY_HOPS` | 엣지 헤더가 없을 때 X-Forwarded-For 오른쪽에서 몇 번째 값을 믿을지 (기본값 0 = 사용 안 함) |
| `OLLAMA_URL` | #13 벤치마크 대상 (기본값 `http://localhost:11434`) |

`claude-opus-5-5` / `claude-sonnet-5-5` 호출에는 서버측 refusal fallback(`fallbacks: "default"`)이 켜져 있습니다.

**#11 CI 게이트** (`.github/workflows/eval.yml`): PR이 프롬프트나 Eval 엔진·데이터를 바꾸면, PR의 프롬프트를 base 브랜치에 있는 같은 프롬프트와 비교합니다. 새 버전 파일(예: `v3.txt`)을 추가했거나 엔진·데이터만 바뀐 PR은 직전 버전 프롬프트를 기준으로 씁니다. 12케이스 LLM 채점은 노이즈가 커서 게이트는 한 케이스 분량의 차이를 허용합니다: Δscore가 허용오차 `max(0.02, 0.3/n)`보다 크게 떨어지거나 순 회귀(회귀−개선)가 2건 이상이면 실패합니다.

## 측정 결과 (LLM 없이 재현 가능)

- **#04 RAG**: 평가 질문 25개 기준 Recall@3은 조합에 따라 60~80%이고, `fixed-200-overlap50 + hybrid-rrf` 조합이 가장 높았습니다. (문서 8개·약 2,500자(5.5KB)를 청크 16~32개로 나눈 소규모 코퍼스입니다. 질문 1개 = 4%p라 1위 0.80과 공동 2위 0.76의 차이는 1문항이고, MRR 기준 1위는 `heading-contextual + char-tfidf`(0.667)입니다. 질문별 실패 목록과 청크 경계 때문에 원리적으로 못 맞히는 문항 수(reachable)도 함께 보여줍니다.)
- **#15 가드레일**: 룰 기반 탐지는 룰을 작성할 때 참고한 dev 세트(공격 25·정상 17)에서 탐지율 96%, 오탐률 0%지만, 룰을 확정한 뒤 추가한 holdout 세트(공격 10·정상 5)에서는 탐지율 10%, 오탐률 60%로 크게 떨어집니다. 룰만으로는 처음 보는 공격(다국어·우회 표현·간접 주입)을 막기 어렵다는 것이 이 측정의 결론이고, LLM 분류기를 함께 두는 이유입니다. 표본이 작아 화면에 95% 신뢰구간을 함께 표시합니다.

## 사용한 실제 데이터와 가상 데이터

`data/handbook`의 회사(루미나랩스)와 규정, 쇼핑몰 DB, 고객 문의, 공격 문장은 모두 데모용으로 만든 **가상 데이터**입니다.

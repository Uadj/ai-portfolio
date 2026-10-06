# AI Engineering Portfolio

LLM API 기초부터 RAG, 에이전트, 평가, 파인튜닝, 프로덕션 게이트웨이, 보안까지 **15개 프로젝트를 한 사이트에서 직접 실행**해 볼 수 있는 포트폴리오입니다.

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
| 13 | 로컬 서빙 벤치마크 | Ollama, TTFT, 처리량, p50/p95 | ✅ (Ollama 필요) |
| 14 | LLM 게이트웨이 | 라우팅, 응답 캐시, 프롬프트 캐싱, 토큰 버킷, 폴백 체인 | |
| 15 | 가드레일 | PII 마스킹(Luhn), 룰+LLM 인젝션 탐지, 카나리 토큰, 방어율 측정 | ✅ 룰 벤치마크 |

## 구조

```
server.py               FastAPI 앱 (projects/*.router 자동 등록)
core/llm.py             Claude 래퍼: 모델, effort, refusal fallback, 사용량·비용 계산
core/sse.py             SSE 헬퍼
projects/p01~p15_*.py   프로젝트별 API
evals/                  #11 Eval 엔진 + 프롬프트 v1/v2
mcp_server/             #09 MCP 서버
p12_finetune/           #12 데이터 합성 / 학습 / 평가 스크립트
scripts/run_eval.py     CI용 Eval 실행기 (회귀 시 exit 1)
data/                   가상 회사 문서, 평가셋, 공격셋, SQLite DB(첫 실행 시 생성)
static/                 프론트엔드 (빌드 없는 바닐라 JS)
```

## 배포 (Render)

위 버튼을 누르면 `render.yaml` 블루프린트로 웹 서비스가 만들어집니다. 배포 화면에서 `ANTHROPIC_API_KEY`만 입력하면 됩니다.

공개 사이트용 비용 보호 장치가 들어 있습니다.
- `DAILY_BUDGET_USD` (기본값 1): 하루 Claude 사용액이 이 금액을 넘으면 LLM 기능을 다음 날까지 멈춥니다. LLM이 필요 없는 측정 기능은 계속 동작합니다.
- `LLM_REQUESTS_PER_HOUR` (기본값 30): IP당 시간당 POST 요청 수를 제한합니다.
- 무료 플랜은 15분 동안 요청이 없으면 잠들고, 첫 요청 때 깨어나는 데 30초 정도 걸립니다. 디스크도 재배포할 때마다 초기화됩니다(MCP 노트, Eval 기록, 게이트웨이 통계).

## 설정

| 변수 | 용도 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API 키 (필수) |
| `CLAUDE_MODEL` | 기본 모델 (기본값 `claude-opus-5-5`) |
| `CLAUDE_FAST_MODEL` | 게이트웨이 경량 라우팅 모델 (기본값 `claude-haiku-4-5`) |
| `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET` | #06 PR 자동 리뷰 |
| `DAILY_BUDGET_USD`, `LLM_REQUESTS_PER_HOUR` | 공개 배포용 비용·남용 방지 (0이면 무제한) |
| `OLLAMA_URL` | #13 벤치마크 대상 (기본값 `http://localhost:11434`) |

`claude-opus-5-5` / `claude-sonnet-5-5` 호출에는 서버측 refusal fallback(`fallbacks: "default"`)이 켜져 있습니다.

## 측정 결과 (LLM 없이 재현 가능)

- **#04 RAG**: 평가 질문 25개 기준 Recall@3은 조합에 따라 60~80%이고, `fixed-200-overlap50 + hybrid-rrf` 조합이 가장 높았습니다.
- **#15 가드레일**: 룰 기반 탐지율 95.8%, 오탐률 0%였습니다. 단, 룰을 이 테스트셋을 보며 작성했기 때문에 학습 데이터 기준의 낙관적인 수치입니다.

## 사용한 실제 데이터와 가상 데이터

`data/handbook`의 회사(루미나랩스)와 규정, 쇼핑몰 DB, 고객 문의, 공격 문장은 모두 데모용으로 만든 **가상 데이터**입니다.

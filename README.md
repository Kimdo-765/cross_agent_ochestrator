# cross-agent-orchestrator (`cao`)

하나의 작업을 **여러 AI 코딩 에이전트가 역할을 나눠** 수행하고, **서로 다른 모델이 실제 `git diff`만 보고 교차 검증**하는
오케스트레이터입니다. Worker가 구현 → Reviewer(다른 모델)가 0~10점 평가 → 기준 미달이면 피드백을 반영해 반복합니다.
모든 과정은 Git 브랜치/워크트리 위에서 이루어지고, 이터레이션별 프롬프트·응답·점수·비용이 기록됩니다.

```
 Task(요청 + 수락 기준)
   │
   ▼  git worktree + branch  cao/<task-id>-<slug>
 ┌──────────────────────────────────────────────────────────────┐
 │  iteration N                                                 │
 │   OFFER → ACK ─▶ Worker (clean context, 코드 수정)            │
 │                   └─ 실제 diff 확인 → git commit → COMMIT     │
 │   OFFER → ACK ─▶ Reviewer (다른 모델, read-only, diff만 전달)  │
 │                   └─ JSON 점수(7개 항목) 파싱 → COMMIT         │
 │   score ≥ 9.0 ─▶ PASS      else ─▶ 피드백을 다음 브리프에 반영  │
 └──────────────────────────────────────────────────────────────┘
   ▼
 PR 생성 | base 브랜치에 머지 | 브랜치 유지  +  report.md / run.json
```

## 지원 백엔드

| 백엔드 | `backend` 키 | 실행 방식 | 인증 |
|---|---|---|---|
| Claude Code | `claude_code` | `claude -p --output-format json` (`--effort`, 리뷰어는 `--tools Read,Grep,Glob…`) | `claude login` 또는 `ANTHROPIC_API_KEY` |
| Codex (OpenAI) | `codex` | `codex exec --json` (`model_reasoning_effort`, 리뷰어는 `--sandbox read-only`) | `codex login` 또는 `OPENAI_API_KEY` |
| Grok (xAI) | `grok` | Codex CLI + xAI OpenAI-호환 프로바이더(`model_providers.xai`) | `XAI_API_KEY` |

Worker와 Reviewer는 **반드시 다른 모델**이어야 합니다(같은 백엔드라도 모델이 다르면 허용, 동일하면 거부).

**모델 목록**은 하드코딩이 아니라 발견(discovery)됩니다 (`cao.loop.catalog`, Web UI의 Model 드롭다운 / `GET /api/models`):

| 백엔드 | 소스 (우선순위순) |
|---|---|
| Codex | `~/.codex/models_cache.json`(Codex CLI가 실제로 보여주는 모델 + 모델별 지원 effort + 기본 모델) → `OPENAI_API_KEY`가 있으면 OpenAI Models API → 정적 폴백 |
| Claude Code | `anthropic` 패키지가 설치·인증돼 있으면 Anthropic Models API → 정적 목록(`claude-opus-5`, `claude-sonnet-5`, … + 별칭 `opus`/`sonnet`/`haiku`) |
| Grok | `XAI_API_KEY`가 있으면 xAI Models API → 정적 폴백 |

드롭다운에 없는 모델은 "Custom model id…"로 직접 입력할 수 있고, effort 목록은 선택한 모델이 지원하는 단계만 표시됩니다(Codex 최신 모델의 `max`/`ultra` 포함; 미지원 모델에 요청하면 지원 최고 단계로 자동 클램프).

## 설치

```sh
git clone https://github.com/Kimdo-765/cross_agent_ochestrator.git && cd cross_agent_ochestrator
pip install -e ".[web]"          # CLI + Web UI
claude --version && codex --version   # 사용할 에이전트 CLI가 PATH에 있어야 함
```

## 빠른 시작 — Web UI (Docker)

```sh
cp .env.example .env             # CAO_WORKSPACE(에이전트가 작업할 저장소들의 상위 폴더), API 키 등 설정
./scripts/start.sh               # 빈 로컬 포트 자동 선택 → 빌드 → web + cloudflared 기동
```

출력 예:
```
cao web UI (local):  http://127.0.0.1:18765
access token:        Qm4…   (sign in: http://127.0.0.1:18765/login?token=Qm4…)
cloudflare tunnel:   https://random-words.trycloudflare.com
remote sign-in:      https://random-words.trycloudflare.com/login?token=Qm4…
```

- 호스트의 `~/.claude`, `~/.codex`, `~/.ssh`가 컨테이너에 마운트되므로 호스트에서 로그인돼 있으면 추가 설정이 필요 없습니다. 에이전트가 작업할 저장소는 `CAO_WORKSPACE` 아래에 두고 UI에서 `/workspace/<repo>`로 지정합니다.
- 터널은 기본이 **퀵 터널**(`--profile tunnel`, 계정 불필요, URL은 매번 바뀜). 고정 주소가 필요하면 `.env`의 `CLOUDFLARE_TUNNEL_TOKEN`에 네임드 터널 토큰을 넣으면 `--profile tunnel-named`가 사용됩니다(Cloudflare 대시보드에서 public hostname → `http://web:8000`).
- 외부에서 접근 가능한 순간부터 **접근 토큰**이 강제됩니다(`CAO_AUTH_TOKEN` 미설정 시 자동 생성·출력). 헬스체크용 `/api/health`만 공개.
- Docker 없이: `./scripts/start.sh --native` (로컬 `cloudflared` 필요) 또는 `cao web --tunnel`.
- 중지: `./scripts/start.sh --stop` · 로그: `docker compose --profile tunnel logs -f`.
- WSL2에서 `docker: command not found`가 나오면 Docker Desktop이 꺼져 있거나 해당 배포판의 WSL integration이 꺼진 것입니다(Settings → Resources → WSL integration).

## 빠른 시작 — CLI

```sh
cd my-project
cao run -w claude_code -r codex \
        -a "POST /items returns 201 with the created id" \
        -a "unit tests cover validation errors" \
        --role coder --worker-effort high --reviewer-effort high \
        -n 5 --pass-score 9 --on-success pr \
        "Add a create-item endpoint to the FastAPI app"
```

```
[cao] task 20260821-1940-fa91 'Add a create-item endpoint…' -- worker=claude_code:default reviewer=codex:default
[cao] worktree …/.cao/worktrees/20260821-1940-fa91 on branch cao/20260821-1940-fa91-add-a-create-item (base main @ e241cba38c)
[cao] [iter 1] worker   OFFER  brief -> claude_code:default (role=coder, attempt 1)
[cao] [iter 1] worker   ACK    worker ready; workspace clean context
[cao] [iter 1] worker   COMMIT committed ca31417a4b;  3 files changed, 84 insertions(+)
[cao] [iter 1] reviewer OFFER  diff (3120 chars) -> codex:default read-only (attempt 1)
[cao] [iter 1] reviewer ACK    reviewer ready; diff-only, read-only
[cao] [iter 1] reviewer COMMIT score weighted=7.85 llm=8.0 issues=2 verdict=request_changes
[cao] [iter 1] score 7.85/10 -> ITERATE (7.85 < 9.0)
[cao] [iter 2] … score 9.30/10 -> PASS (9.30 >= 9.0)
[cao] [iter 2] finish   COMMIT PR opened: https://github.com/…/pull/12
```

자주 쓰는 옵션: `-C <repo>`(없으면 `git init`까지 수행) · `--base <branch>` · `--scoring weighted|llm` · `--stop-if-no-progress N` ·
`--budget <USD>` · `--require-tests` · `--on-success pr|merge|none` · `--dry-run` · `--json`.
`cao tasks` / `cao tasks <id> --logs` 로 기록을 조회합니다.

## 핵심 개념

| 개념 | 구현 |
|---|---|
| **Task** | 요청 + 수락 기준(acceptance criteria) + worker/reviewer 설정 + 루프 설정 (`TaskSpec`) |
| **Worker** | 코드를 실제로 수정하는 에이전트. 매 이터레이션 **새 프로세스 = 깨끗한 컨텍스트**. 역할 프리셋: `coder` `planner` `tester` `security` `refactorer` `docs` + 자유 지시문 |
| **Reviewer** | Worker와 다른 모델. **읽기 전용**으로 실행되며 프롬프트에는 Worker의 요약이 아닌 **실제 `git diff <base>..HEAD`** 만 들어감. 7개 항목 0~10점 + 이슈 목록 JSON 반환 |
| **Orchestrator** | 워크트리/브랜치 생성, 핸드셰이크, 커밋, 점수 판정, 조기 종료, PR/머지, 로그·비용 기록 (`LoopEngine`) |
| **Handshake** | 모든 핸드오프가 `OFFER → ACK/NACK → COMMIT`. ACK/COMMIT은 에이전트의 주장이 아니라 **검증 가능한 사실**(CLI 존재, 모델 상이, diff 비어있지 않음, JSON 파싱 성공, 리뷰어가 파일을 건드리지 않음)로만 결정. NACK 시 재OFFER(`handshake_retries`) |
| **Iteration** | Worker → diff → commit → Reviewer → score → 판정. 이터레이션별 `worker.prompt.md`, `worker.response.md`, `diff.patch`, `review.prompt.md`, `review.json` 저장 |

### 리뷰 평가 항목과 점수

| 항목 | 기본 가중치 |
|---|---|
| 요구사항 충족도 (`requirements`) | 2.0 |
| 코드 정확성 / 버그 가능성 (`correctness`) | 2.0 |
| 보안 이슈 (`security`) | 1.5 |
| 기존 스타일 / 아키텍처 일관성 (`consistency`) | 1.0 |
| 테스트 커버리지 (`tests`) | 1.5 |
| 불필요한 변경 여부 (`minimality`) | 0.5 |
| 기존 기능 정상 작동 (`regression`) | 1.5 |

최종 점수는 `scoring: weighted`(가중 평균, 가중치 조정 가능) 또는 `scoring: llm`(리뷰어의 종합 점수). `blocker` 이슈가 있으면 6점을 넘지 못하고,
리뷰어 verdict가 `request_changes`면 통과선 바로 아래로 캡핑됩니다(`respect_verdict`, 끄려면 `--ignore-verdict`).
**≥ pass_score(기본 9.0) → 완료**, 미만이면 이슈 목록이 우선순위대로 다음 Worker 브리프에 들어갑니다.

### 종료 조건

- 통과: `score ≥ pass_score`
- `max_iterations` 도달 → `exhausted`
- `stop_if_no_progress` 이터레이션 동안 점수 개선 없음 → `stopped`
- `budget_usd` 초과 → `stopped` (CLI가 비용을 보고하는 경우; Claude Code는 USD, Codex는 토큰)
- 핸드셰이크 재시도 소진(Worker가 아무것도 바꾸지 않음 / `blocked` / Reviewer JSON 불량 등) → `failed`

## 결과물

```
<repo>/.cao/
├── worktrees/<task-id>/           # Worker가 작업한 워크트리 (브랜치 cao/<task-id>-<slug>)
└── tasks/<task-id>/
    ├── iteration-01/
    │   ├── worker.prompt.md  worker.response.md  diff.patch
    │   ├── review.prompt.md  review.response.md  review.json
    │   └── logs/             # CLI stdout/stderr 원본
    ├── report.md             # 사람이 읽는 요약 (PR 본문으로도 사용)
    └── run.json              # 전체 상태 (이터레이션, 핸드셰이크 이벤트, 비용, 토큰)
~/.cao/cao.db                 # SQLite: 작업/이터레이션/로그 (Web UI와 CLI가 공유)
```

## Web UI

- **New task**: 요청·수락 기준·저장소 경로(브라우저로 선택, 없으면 생성)·Worker(백엔드/모델/effort/역할/추가 지시)·Reviewer(백엔드/모델/effort)·루프 설정. 같은 모델을 고르면 즉시 경고.
- **Task 상세**: 이터레이션 카드(7개 항목 점수 바, 이슈, diff, 핸드셰이크 이벤트, Worker/Reviewer 프롬프트·응답·비용), 실시간 로그(SSE), 취소/재실행/복제/삭제, PR 링크.
- REST API: `/api/docs` (OpenAPI). 예: `POST /api/tasks`, `GET /api/tasks/{id}/events`(SSE), `GET /api/tasks/{id}/iterations/{n}/diff`.

## 설정 레퍼런스 (API / `--dry-run` 출력 형식)

```json
{
  "title": "Add create-item endpoint",
  "request": "…",
  "acceptance_criteria": ["…", "…"],
  "repo_path": "/workspace/my-project",
  "base_branch": null,
  "worker":   {"backend": "claude_code", "model": null, "effort": "high", "role": "coder", "instructions": "", "timeout": 1800},
  "reviewer": {"backend": "codex", "model": null, "effort": "high", "role": "reviewer", "timeout": 1800},
  "loop": {"max_iterations": 5, "pass_score": 9.0, "scoring": "weighted", "weights": {"tests": 2.0},
           "stop_if_no_progress": 2, "budget_usd": null, "on_success": "pr", "handshake_retries": 1, "require_tests": false}
}
```

## 다중 에이전트 플로우 (`cao flow`)

리뷰 루프 외에, `cao.yaml`로 정의하는 병렬/파이프라인/플랜 전략도 제공합니다:
`cao init` → `cao agents` → `cao flow run -w compare "질문"`. 자세한 건 `cao flow run --help`.

## 개발

```sh
pip install -e ".[dev]"
pytest -q          # 실제 CLI 없이 tests/fake_bins 의 가짜 claude/codex 로 루프 전체를 검증 (67 tests)
```

## 로드맵

- [ ] Reviewer 2인 합의(서로 다른 두 모델의 평균/최소 점수)
- [ ] Worker 역할을 이터레이션마다 바꾸는 시퀀스(planner → coder → tester)
- [ ] 워크트리 정리 명령(`cao gc`) 및 브랜치 일괄 삭제
- [ ] 스트리밍(`stream-json`)으로 에이전트 진행 상황 실시간 표시

## 라이선스

MIT

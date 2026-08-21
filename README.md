# cross-agent-orchestrator (`cao`)

서로 다른 코딩 에이전트 CLI — **Claude Code**, **Codex**, **Gemini CLI**, 혹은 임의의 커맨드 — 를
하나의 목표(goal)에 함께 투입하고 조율하는 오케스트레이터입니다.

```
cao run -w compare "이 저장소에서 가장 위험한 기술 부채는?"
   ├─ claude ──┐
   ├─ codex  ──┼─▶ synthesizer(claude) ─▶ 하나의 최종 답변 + 리포트
   └─ gemini ──┘
```

## 왜 필요한가

- 같은 질문을 여러 에이전트에 던져 **교차 검증**하고 최선의 답을 합성합니다 (`parallel`).
- 한 에이전트가 구현하고 다른 에이전트가 **리뷰·수정**하는 핸드오프를 자동화합니다 (`pipeline`).
- 플래너가 큰 목표를 쪼개고, 워커들이 **각자의 git worktree에서 병렬로** 작업한 뒤 통합 리포트를 받습니다 (`plan`).
- 모든 실행 로그·사용량·결과가 `.cao/runs/<run-id>/`에 남습니다.

## 설치

```sh
pip install -e .          # 개발 설치 (저장소 루트에서)
# 또는
pipx install git+https://github.com/Kimdo-765/cross_agent_ochestrator.git
```

요구 사항: Python 3.10+, 그리고 사용하려는 에이전트 CLI가 PATH에 있고 로그인되어 있어야 합니다.

| 에이전트 | `type` | 실행 방식 | 설치 |
|---|---|---|---|
| Claude Code | `claude_code` | `claude -p --output-format json` | `npm i -g @anthropic-ai/claude-code` |
| Codex CLI | `codex` | `codex exec --json -o …` | `npm i -g @openai/codex` |
| Gemini CLI | `gemini` | `gemini -p …` | `npm i -g @google/gemini-cli` |
| 아무 커맨드 | `shell` | `options.command` 템플릿 | — |

## 빠른 시작

```sh
cd my-project
cao init                # cao.yaml 예제 생성
cao agents --versions   # CLI 설치/로그인 상태 확인
cao run -w compare "Explain the auth flow in this repo and point out risks."
```

설정 파일 없이 즉석으로도 실행할 수 있습니다:

```sh
cao run -a claude,codex                 "Reply with one word: pong"        # parallel (기본)
cao run -a codex,claude -s pipeline     "Add type hints to utils.py"       # codex 구현 → claude 리뷰
cao run -a claude,codex -s plan         "Split the monolith module into a package"
```

## 설정 (`cao.yaml`)

```yaml
defaults:
  timeout: 1800          # 에이전트 1회 실행 제한(초)
  isolation: worktree    # shared | worktree | none
  synthesizer: claude    # parallel/plan 결과를 합치는 에이전트

agents:
  claude:
    type: claude_code
    model: claude-sonnet-5          # 생략 시 CLI 기본값
    tags: [review, architecture]    # 플래너가 작업 배정 시 참고
    options:
      permission_mode: acceptEdits
      max_turns: 40
  codex:
    type: codex
    tags: [implementation]
    options:
      sandbox: workspace-write
  my-agent:                          # 임의 커맨드도 에이전트로
    type: shell
    options:
      command: ["my-agent", "--task", "{prompt}"]   # {prompt} {workdir} {task_id} {model}
      prompt_via: arg                                # 또는 stdin

workflows:
  compare:
    strategy: parallel
    agents: [claude, codex]
    isolation: none                  # 질의응답이면 저장소 접근 불필요
    prompt: "{goal}"

  implement-then-review:
    strategy: pipeline
    isolation: shared
    steps:
      - agent: codex
        prompt: "Implement: {goal}. End with a summary."
      - agent: claude
        prompt: "Review the diff for: {goal}\n\nImplementer said:\n{previous}"

  build:
    strategy: plan
    planner: claude
    workers: [claude, codex]
    synthesizer: claude
    max_tasks: 5
```

`${ENV_VAR}` / `${ENV_VAR:-default}` 문법으로 환경 변수를 참조할 수 있습니다.

### 전략

| strategy | 동작 | `{placeholder}` |
|---|---|---|
| `parallel` | 같은 프롬프트를 `agents` 전부에 동시 실행 → `synthesizer`가 비교·합성 | `{goal}` |
| `pipeline` | `steps`를 순서대로 실행, 이전 단계 출력이 다음 단계 입력 | `{goal}`, `{previous}` |
| `plan` | `planner`가 JSON 작업 목록 생성 → `workers`에 분배·병렬 실행 → `synthesizer`가 통합 리포트 | (플래너 프롬프트 내장) |

### 격리(isolation)

| 값 | 의미 |
|---|---|
| `shared` | 프로젝트 디렉토리에서 직접 실행. 순차(pipeline)에 적합 |
| `worktree` | 작업마다 `cao/<run-id>/<task>` 브랜치 + git worktree 생성. 병렬 편집에 안전. 브랜치는 남겨두므로 `git diff HEAD..cao/...`로 검토 후 머지 |
| `none` | 임시 디렉토리에서 실행(저장소 접근 없음). 순수 Q&A용 |

## 결과물

```
.cao/runs/20260821-191500-c116/
├── goal.md
├── plan.json          # plan 전략일 때
├── logs/              # 에이전트별 stdout/stderr 전체 기록
├── report.json        # 구조화된 결과 (usage/cost 포함)
└── report.md          # 사람이 읽는 리포트
```

`cao runs` 로 과거 실행 목록을, `cao run --json` 으로 결과를 JSON으로 받을 수 있습니다.

## 어댑터 확장

```python
from cao.adapters import register, AgentAdapter

@register
class MyAdapter(AgentAdapter):
    key = "my_agent"        # cao.yaml 의 type:
    binary = "my-agent"

    def build_command(self, task, workdir, run_dir):
        return [self.executable(), "--prompt", task.prompt], None   # (argv, stdin)

    def parse_output(self, proc, run_dir):
        return proc.stdout.strip(), {}, {}                          # (text, usage, raw)
```

## 개발

```sh
pip install -e ".[dev]"
pytest -q      # 실제 CLI 없이 tests/fake_bins 의 가짜 claude/codex 로 검증
```

## 로드맵

- [ ] `debate` 전략 (에이전트 간 상호 비평 라운드)
- [ ] worktree 브랜치 자동 머지 / 충돌 리포트
- [ ] 실행 중 스트리밍 출력(`stream-json`) 표시
- [ ] 에이전트별 비용 상한 및 재시도 정책

## 라이선스

MIT

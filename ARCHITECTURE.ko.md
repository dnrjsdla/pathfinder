# ARCHITECTURE (한국어본)

**지금 코드가 하는 일만 적는다.** 왜 이렇게 생겼는지는 `docs/blueprint/` 와
`docs/decisions/DECISIONS.md`, 실측이 바꾼 것은 `HISTORY.md`. 여기 적힌 이름은 전부 트리에
실존한다. 정본은 영어 `ARCHITECTURE.md` 이고 이 파일은 같은 커밋에서 함께 고친다.

지금까지 지은 것: **구획 2 — 손.** 비서가 말하고(1), 이제 툴을 부르고 · 셸을 샌드박스에 가두고 ·
쓰기 전에 묻고 · 도는 중에 한 말을 받고 · 멈추라면 멈춘다(2). 신호 · 작업자 · 기억 · 설치기는
아직 없다.

## 0. 한 문장

모든 사실은 먼저 장부 하나에 적히고, 나머지는 전부 거기서 파생되며 지워도 된다.

```
클라이언트 (nc -U, `pathfinder say|approve|deny|interrupt`)
   │  <홈>/pathfinder.sock 위의 NDJSON  (0600, TCP 없음)
   ▼
daemon.Daemon ── 세션마다 lane 하나 (큐 + 워커); 큐 항목: say | resume | continue
   │  record(): event = ledger.append(...)  →  state.apply(event)      (이벤트 루프 스레드에서만)
   ├─▶ ledger.Ledger   <홈>/events/<UTC 날짜>.jsonl   append-only, 진실
   ├─▶ state.State     <홈>/state.db                  파생, 다시 만들 수 있음 (메시지 · 승인)
   └─▶ loop.run(step, messages, call_tool, emit, control)   워커 스레드에서
            ├─ step = llm.openai_compat.stream(endpoint, messages, tools, cancel=control)
            └─ call_tool = broker.Broker.call ── coerce → normalize → policy.decide → 핸들러
                                                   allow: 실행 · ask: Gate 예외 · deny: DENIED_TEXT
                     tools.TOOLS: run_command (exec.py, Seatbelt) · read_file · write_file
```

## 1. 무엇이 어디에 사나

| 자리 | 무엇 | 쓰는 길 | 지우면 |
|---|---|---|---|
| `<홈>/events/<날짜>.jsonl` | 사건 전부, 한 줄에 JSON 하나 | `ledger.Ledger.append()` 하나뿐 | 기록을 잃는다 — 유일한 진실 |
| `<홈>/state.db` | 세션 · 메시지 · 커서 · 승인 | `state.State.apply()` | `pathfinder reindex`, 또는 그냥 재시작 |
| `<홈>/config.json` | 모델 이름 · provider · `grants` · 손잡이 | `config.set()` 하나뿐 | 기본값(모델 미설정) |
| `<홈>/workspace/` | 샌드박스 셸이 쓸 수 있는 유일한 곳 · 모든 셸 명령의 cwd · 상대경로 파일이 놓이는 곳 | 사용자의 요청으로 툴이 | PATHFINDER 의 것은 없음 |
| `<홈>/pathfinder.sock` | 데몬의 소켓 | 데몬 | 뜰 때 다시 만든다 |

`<홈>` 은 `$PATHFINDER_HOME` 또는 `~/.pathfinder` (권한 0700).

## 2. 장부 (`pathfinder/ledger.py`)

- `partition(ts)` 가 시각을 UTC 날짜로 바꾼다. **시각이 파일 이름이 되는 자리는 여기 하나뿐이다.**
  읽는 쪽은 날짜를 계산하지 않는다 — 파티션을 순서대로 훑고 `seq` 로 거른다.
- 사건은 예약 칸 `seq, ts, type, session, turn, origin, trust` 와 데이터로 이뤄진다. `seq` 는
  전역이고 엄격히 증가한다. `type` · `origin` · `trust` 는 닫힌 집합이다.
- 프로세스 둘이 쓸 수 있다(데몬, 그리고 `config set` 을 도는 CLI): append 마다 스레드 잠금과
  `events/.lock` 의 배타 `flock` 을 잡고, 최신 파티션 크기가 바뀌었으면 꼬리에서 `seq` 를 다시 구한다.
- 죽었다 살아날 때: 마지막 `seq` 는 파티션을 최신부터 거꾸로 훑어 온전한 줄이 나올 때까지
  찾는다. 개행 없이 끝난 조각 뒤에는 개행을 먼저 넣고 쓴다. `read()` 는 못 읽는 줄을 건너뛴다.
- 알려진 천장: `fsync` 없이 `flush()` 만 한다.

지금의 사건 종류: `session_start, user, assistant, tool_call, tool_result, turn_start, turn_end,
error, config_set, approval_pending, approval, interrupted`. `config_set` 은 키만 적는다. 턴 도중의
steer 는 `steer: true` 가 붙은 `user` 사건이다. `tool_call` 의 인자와 `tool_result` 의 결과는
2,000자로 자른다. `approval_pending` 은 도는 중이던 메시지를 통째로 싣는다(재개에 필요).

## 3. 파생 상태 (`pathfinder/state.py`)

`apply(event)` 가 유일한 투영이고 실시간 쓰기와 재구축이 같은 함수를 지난다. 멱등이며
(`seq <= cursor` 무시) `BEGIN IMMEDIATE` 와 잠금 아래 있어 프로세스 둘이 한 턴을 두 번 세지
못한다. `gated` 로 끝난 턴은 교환 횟수에 들지 않는다.

**말한 것만 이력이 된다:** `user` 사건은 `origin == "user"` 일 때(steer 포함), `assistant` 사건은
`final` 일 때만. 표 `approvals(id, call_id, session, turn, tool, args_json, messages_json, ts,
decision)`: `approval_pending` 이 넣고 `approval` 이 결정을 적는다. `pending()` · `approval(id)` 로
읽는다. `catch_up(ledger)` 는 데몬이 뜰 때, `rebuild(ledger)` 는 표를 버리고 다시 흘린다 —
승인 대기도 함께 돌아온다.

## 4. 모델 층 (`pathfinder/llm/`)

- `resolve(cfg, "provider:model")` 은 첫 콜론에서 갈라 `Endpoint` 를 돌려준다. 모델 이름은
  `config.json` 에만 있고, 미설정이면 분명한 오류가 난다.
- `openai_compat.stream(endpoint, messages, tools, cancel)` 은 OpenAI 호환 SSE 를 말하고
  `text | reasoning | tool_calls | usage | cancelled` 를 낸다. `cancel` 은 `is_set()` 이 있는
  무엇이든 되고 청크 사이에서 확인한다. 실패는 전부 `LLMError`, 402 · 429 는 `blocked`. 데몬의
  step factory 는 툴 스키마를 항상 넘긴다 — 모델이 자기 손을 봐야 한다(첫 라이브 실행에서
  안 넘기고 있던 것이 잡혔다 — `test_default_step_factory_sends_tool_schemas`).

## 5. 툴 · 정책 · 샌드박스 · 브로커

**`tools.py`** — `ToolSpec(name, description, params, required, handler, write, network,
sandboxed, normalize)`; `schema()` 는 OpenAI 툴 dict; `coerce(spec, args)` 는 선언된 인자를
JSON 스키마 타입으로 캐스팅하고(문자열 `"false"` 가 참으로 통과해 escalate 가 되면 안 된다),
선언 안 된 키를 버리고, 빠진 필수 키를 오류로 낸다. `normalize` 는 coerce 뒤 · policy 앞에 돌아
**게이트에 보이는 것이 실제로 일어날 일**이 되게 한다: `read_file` · `write_file` 의 상대경로는
`<workspace>/경로` 가 되고 `~` 는 펼쳐진다. 툴 셋: `run_command(command, escalate)`(write,
sandboxed) · `read_file(path)` · `write_file(path, content)`(write). 파일 툴 둘은
`exec.SECRET_PATHS` 를 거부한다.

**`policy.py`** — `decide(spec, origin, args, grants) -> allow | ask | deny`. 모델 없음, 셸 문법
해석 없음, 읽는 인자는 `escalate` 하나. 샌드박스 툴은 `sandboxed` 로 판정하되 escalate 하거나
이 기계에 샌드박스가 없으면 `write` 다.

| 출처 \ 툴 | read | sandboxed | network | write |
|---|---|---|---|---|
| `user` | allow | allow | allow | **ask** |
| `signal` | allow | ask | ask | ask |
| `job` | allow | ask | ask | ask |
| `system` | allow | deny | deny | deny |

`config.json` 의 `grants`(`{툴: allow|ask|deny}`) 는 `user` 턴에서만 표를 이기고, 상자를 벗어난
샌드박스 툴(escalate, 샌드박스 없음)에는 절대 적용되지 않으며, 세 낱말 중 하나일 때만 먹는다 —
config 가 다른 값을 거부하고 policy 도 무시한다. 오타가 게이트를 끄면 안 된다. `DENIED_TEXT` 는
실패가 아니라 정책이라고 말해 모델이 우회로를 찾지 않게 한다.

**`exec.py`** — Mark.5 의 Seatbelt 프로파일(실측은 HISTORY): `(deny default)`, 프로세스
exec/fork, HOME 아래 `SECRET_PATHS`(`.ssh .aws .gnupg .config/gh .docker Library/Keychains
Library/Cookies .netrc .npmrc .git-credentials` 셸 rc 파일들 `.env*`) 를 뺀 전체 읽기, 네트워크
열림, 쓰기는 workspace · TMPDIR · `/private/tmp` · dev 노드만. 자식 환경변수는 허용 목록
(`PATH HOME LANG LC_ALL LC_CTYPE TZ TMPDIR`). `run_command(command, escalate, control,
timeout=60)`: 자기 세션의 `Popen`, `control.interrupt` 가 서는 순간 감시 스레드가 프로세스 그룹을
통째로 죽인다. 출력은 8,000자로 자르고, 0 아닌 종료 코드는 답이며, "not permitted" 에는
샌드박스가 막았다는 힌트가 붙는다. `available()` 은 darwin + `/usr/bin/sandbox-exec`.

**`broker.py`** — `Broker(tools, origin, grants, control, emit, approved, denied)`;
`call(id, name, args)` 가 루프의 `call_tool` 이다: 모르는 툴 → 오류(거부와 다른 문구); coerce →
오류; 거부된 id → `USER_DENIED_TEXT`; 승인된 id 는 `(name, args)` 가 사용자가 승인한 것과 같을 때
한 번만 돌고 소비된다; 그 밖에는 `policy.decide`: deny → `DENIED_TEXT`, ask → `Gate(id, name,
arguments)` 예외, allow → `emit("tool_call")`, `control` 을 들고 핸들러 실행, `emit("tool_result")`.
브로커는 장부를 쓰지 않는다. `Gate.arguments` 이지 `.args` 가 아니다 — `BaseException.args` 는
dict 를 키 튜플로 바꿔 버린다.

## 6. 루프 (`pathfinder/loop.py`)

`run(step, messages, *, call_tool, emit, budget, control, resume)` 이 한 턴을
`Outcome("answer" | "exhausted" | "cancelled")` 로 끝낸다. 게이트는 결말이 아니라 예외다.
`TurnControl` 이 손잡이 하나: `interrupt` · `steer_cut` 이벤트, `steers` deque, `in_tool`;
`is_set()` 은 둘 중 하나라 어댑터의 `cancel=` 에 그대로 들어간다.

- 모델을 부르기 전마다: 쌓인 steer 를 맨 user 메시지 하나로 비우고, `steer_cut` 을 내리고,
  `interrupt` 가 서 있으면 `cancelled` 로 돌아간다 — 실제 어댑터는 요청이 나간 뒤에야 `cancel`
  을 보므로, 이 확인이 interrupt 뒤 요청 하나를 안 쓰게 한다.
- `steer_cut` 이 선 채 `cancelled` 로 끝난 스트림은 **도중 steer** 다: 쓴 데까지를 assistant
  메시지로 남기고, steer 를 `STEER_ENVELOPE`(m2 에서 잰 그 문자열) 안에 넣어 붙이고 계속 돈다.
  `interrupt` 가 서 있으면 취소다.
- `settle(messages, call_tool, control)` 은 마지막 assistant 메시지의 툴 호출 중 아직 결과가
  없는 것을 순서대로 실행한다(각각 `in_tool` 을 세워 둔 채). `resume=True` 면 먼저 settle 한다.
  settle 에서 나온 `Gate` 는 그때까지 흘린 텍스트 `said` 를 싣고 올라간다.
- 어떤 결말이든 그 턴에 흘려보낸 텍스트를 전부 싣는다. reasoning 은 이력에 되돌리지 않는다.

## 7. 데몬 (`pathfinder/daemon.py`)

- 규약, 한 줄에 JSON 하나. 들어오는 것 `attach {session?}` · `say {text}` · `interrupt` ·
  `approve {id}` · `deny {id}`. 나가는 것 `attached` · `turn.start` · `delta` · `tool {name, ok}` ·
  `gate {id, tool, args}` · `turn.end {kind}` · `error`. 깨진 줄 · 모르는 종류 · 없거나 이미 결정된
  승인 id · 도는 턴이 없는 interrupt 는 `error` 한 줄이고 연결은 안 끊는다.
- 세션 = 큐 + 워커 하나. 큐 항목: `("say", text)` · `("resume", approval_id)` ·
  `("continue", steer_text)`. `attach` 는 `session_resume_s` 안의 최근 세션을 잇는다.
- **턴이 도는 중의 `say` 는 steer 다**(D17): `user {steer: true}` 로 적히고 `control.steers` 에
  들어가며, 툴 핸들러가 도는 중(`in_tool`)이 아니면 `steer_cut` 이 선다 — 스트림은 끊지만 툴은
  안 끊는다. 모델의 마지막 청크 뒤에 도착한 steer 도 잃지 않는다: 턴이 끝났는데 steer 가 남아
  있으면 lane 이 `continue` 턴을 돌려 그것을 마지막 user 메시지로 붙인다(다시 적지는 않는다).
- **interrupt** 는 `control.interrupt` 를 세운다: 셸 감시자가 프로세스 그룹을 죽이고, 어댑터는
  다음 청크에서 멈추고, 루프는 `cancelled` 를 낸다. 데몬은 `interrupted` → `assistant {final}`
  (말한 것) → `turn_end {kind: "cancelled"}` 순으로 적는다.
- **게이트**(D16): `Gate` 가 워커에서 올라오면 데몬은 그때까지 말한 것을 `assistant {final}` 로
  적고, 남은 steer 를 메시지에 비워 넣고, **자기 id** 로 `approval_pending {id, call_id, tool,
  args, messages}` 를 적는다(모델의 호출 id 는 턴마다 반복되는, 모델이 고른 문자열이다).
  `gate` 를 보내고 `turn_end {kind: "gated"}` 를 적는다. lane 은 비어 있다. `approve`/`deny` 는
  state 에 대기 중인 id 여야 하고, `approval {id, decision}` 을 적고 그 승인의 세션 lane 에
  `resume` 을 넣는다. 재개된 턴은 저장된 메시지를 `Broker(approved={call_id: (tool, args)})`
  또는 `denied={call_id}` 와 `loop.run(resume=True)` 로 다시 돌린다. 재개된 턴도 다시 게이트에
  걸릴 수 있다.
- 워커 스레드의 `emit` 종류: `delta` · `tool_call` · `tool_result`; 이벤트 루프 스레드가 뒤 둘을
  장부에 적고 `tool` 을 보낸다. 장부와 상태에 쓰는 일은 전부 그 스레드에서 한다.
- 모델이 막혀도 데몬은 산다: `LLMError` 는 `error` 사건 · `error` 줄 · `turn.end {kind: "error"}`.
  소켓은 0600 으로 태어나고, 살아 있는 소켓이면 거부한다.

## 8. 불변조건과 그것을 붙드는 테스트

| 불변조건 | 테스트 |
|---|---|
| 장부에 쓰는 길 하나 · 파티션 함수 하나 · 설정에 쓰는 길 하나 · 날짜 계산은 다른 데 없음 | `test_invariants.test_single_writer` |
| 닫힌 어휘 · 13일 세션이 통째로 읽힘 · 두 프로세스가 같은 `seq` 안 냄 · 잘린 최신 파티션에서도 `seq` 가 삶 | `test_ledger.*` |
| 상태는 다시 만들 수 있고 멱등이며 턴은 한 번만 세어짐 · 말한 것만 기억 | `test_state.*` |
| 대화와 **승인 대기**가 `state.db` 삭제를 견딤 | `test_daemon.test_daemon_resumes_after_state_deleted`, `test_reindex_restores_pending_approval` |
| 모델이 막히거나 미설정이어도 데몬은 삶 | `test_daemon.test_daemon_survives_model_402`, `test_unset_model_is_an_error_not_a_crash` |
| 실제 step factory 가 툴 스키마와 control 을 넘김 | `test_daemon.RealStepFactoryTest` |
| 게이트가 데몬 재시작을 견딤 · 거부는 거부로 재개 · 같은 호출 id 의 게이트 둘이 다 해결되고 게이트 문구가 이력에 남음 | `test_daemon.test_daemon_gate_survives_restart`, `test_daemon_deny_resumes_with_denial`, `ReviewRegressionsTest` |
| 승인은 한 번뿐이고 정확히 그 호출에만 | `test_broker.test_broker_approval_is_single_use_and_exact` |
| 정책표 칸마다 · grant 는 상자를 벗어난 셸을 덮지 않음 · 오타는 닫힘 | `test_policy.*`, `test_config` |
| 툴 경계의 steer 는 맨 user 메시지 · 도중 steer 는 쓴 데까지 남기고 봉투를 입음 · 마지막 청크 뒤의 steer 는 제 턴을 받음 | `test_loop.test_loop_steer_*`, `test_daemon.test_daemon_say_during_turn_steers`, `test_steer_after_the_last_chunk_gets_its_own_turn` |
| interrupt 는 1초 안에 프로세스 그룹을 죽이고(맨몸 · 샌드박스) 요청을 더 안 씀 | `test_exec.test_exec_interrupt_*`, `test_loop.test_loop_interrupt_before_a_step_spends_no_request`, `test_daemon.test_daemon_interrupt_kills_tool` |
| 샌드박스가 workspace 밖 쓰기와 급소 읽기를 막음 · 파일 툴이 급소 경로를 거부 | `test_exec.*`, `test_tools.*` |
| 예산이 바닥나도 흘려보낸 말은 이력에 남음 · 스트림 도중 실패도 `LLMError` · reasoning 을 되돌리지 않음 | `test_daemon.test_exhausted_turn_still_records_what_was_said`, `test_llm.*`, `test_loop.test_loop_does_not_echo_reasoning` |
| 소켓 0600 · 유닉스 소켓만 · 소스에 모델 ID · TCP 없음 | `test_daemon.test_socket_perms`, `test_invariants.test_no_model_ids_or_tcp_in_source` |

## 9. 세는 법

```bash
python3 -m unittest discover -s tests            # 전체 테스트, 네트워크 없음 (84)
wc -l pathfinder/*.py                            # 코어; 배분: ledger 400 · state 600 · daemon 600 · loop 400 · broker+policy 350 · exec 300 · tools 300
python3 scripts/scan_personal.py --all           # 추적 파일 전체 개인정보 스캔
```

## 10. 돌리는 법

```bash
python3 -m pathfinder config set model provider:model-name
python3 -m pathfinder daemon
python3 -m pathfinder say "안녕"                  # 답이 흐르는 중의 두 번째 `say` 는 steer
python3 -m pathfinder approvals                   # 승인 대기 목록, state.db 에서 읽음
python3 -m pathfinder approve ID | deny ID        # 게이트에 걸린 턴을 재개하고 답을 출력
python3 -m pathfinder interrupt                   # 최근 세션의 도는 턴을 멈춤
python3 -m pathfinder config set grants '{"write_file": "allow"}'
python3 -m pathfinder reindex                     # 데몬이 도는 중에는 거부
```

# ARCHITECTURE (한국어본)

**지금 코드가 하는 일만 적는다.** 왜 이렇게 생겼는지는 `docs/blueprint/` 와
`docs/decisions/DECISIONS.md`, 실측이 바꾼 것은 `HISTORY.md`. 여기 적힌 이름은 전부 트리에
실존한다. 정본은 영어 `ARCHITECTURE.md` 이고 이 파일은 같은 커밋에서 함께 고친다.

지금까지 지은 것: **구획 3(그 착수 순서의 2단계) — 세상이 말한다.** 비서가 말하고(1) 툴을
부르고(2); 이제 사용자가 정한 일정, 실패하거나 끝난 셸 명령이 `Signal` 이 된다. 값싼 규칙
(`RULES.md`) 이 speak · silence · defer · ask_model 을 공짜로 정하고, `speak` 만 짧은 모델 턴을
열며, 그 알림은 세션이 비기를 기다렸다가 나간다. 캘린더 · 메일 · `attach`/`claude_code` 감각과
울타리 친 판정의 모델 호출은 다음 단계이고, 작업자 · 기억 볼트 · TUI 는 그 뒤 구획들이다.

## 0. 한 문장

모든 사실은 먼저 장부 하나에 적히고, 나머지는 전부 거기서 파생되며 지워도 된다.

```
클라이언트 (nc -U, `pathfinder say|listen|signal|react|approve|deny|interrupt`)
   │  <홈>/pathfinder.sock 위의 NDJSON  (0600, TCP 없음)
   ▼
daemon.Daemon ── 세션마다 lane 하나 (큐 + 워커); 큐 항목: say | resume | continue | signal
   │  record(): event = ledger.append(...)  →  state.apply(event)      (이벤트 루프 스레드에서만)
   ├─▶ ledger.Ledger   <홈>/events/<UTC 날짜>.jsonl   append-only, 진실
   ├─▶ state.State     <홈>/state.db                  파생, 다시 만들 수 있음 (메시지 · 승인 ·
   │                                                    signals · decisions · judgments · deliveries)
   ├─▶ loop.run(step, messages, call_tool, emit, control)   워커 스레드에서 (유저 턴과 신호 턴 모두)
   │        ├─ step = llm.openai_compat.stream(endpoint, messages, tools, cancel=control)
   │        └─ call_tool = broker.Broker.call ── coerce → normalize → policy.decide → 핸들러
   │                                               allow: 실행 · ask: Gate 예외 · deny: DENIED_TEXT
   │                 tools.TOOLS: run_command · read_file · write_file · schedule 의 add/list/remove
   ├─▶ signals.SenseLoop ── senses/schedule.py 를 자기 주기로 폴링; senses/shell.py 는 push 만
   │        push_signal → 중복 제거 (state.seen) → lane 의 "signal" 항목 → rules.decide → 말할까?
   │        → 위의 그 모델 턴 하나 (origin "signal", Signal-turns 절, 이력 없음, budget 4)
   └─▶ deliver.Outbox ── 턴이 도는 중이거나 presence 가 "타이핑 중"이면 알림을 들고 있는다;
            모든 turn.end 와 presence 갱신 뒤 풀린다; 붙은 클라이언트가 있으면 소켓, 없으면 배너
```

## 1. 무엇이 어디에 사나

| 자리 | 무엇 | 쓰는 길 | 지우면 |
|---|---|---|---|
| `<홈>/events/<날짜>.jsonl` | 사건 전부, 한 줄에 JSON 하나 | `ledger.Ledger.append()` 하나뿐 | 기록을 잃는다 — 유일한 진실 |
| `<홈>/state.db` | 세션 · 메시지 · 커서 · 승인 | `state.State.apply()` | `pathfinder reindex`, 또는 그냥 재시작 |
| `<홈>/config.json` | 모델 이름 · provider · `grants` · `senses` · `schedules` · 손잡이 | `config.set()` 하나뿐 | 기본값(모델 미설정) |
| `<홈>/workspace/` | 샌드박스 셸이 쓸 수 있는 유일한 곳 · 모든 셸 명령의 cwd · 상대경로 파일이 놓이는 곳 | 사용자의 요청으로 툴이 | PATHFINDER 의 것은 없음 |
| `<홈>/pathfinder.sock` | 데몬의 소켓 | 데몬 | 뜰 때 다시 만든다 |
| `<홈>/RULES.md` | speak/silence/defer/ask_model 규칙, 사람이 읽을 수 있는 한 장 | 첫 시작에 `pathfinder/prompts/RULES.md` 에서 만들어짐; 손으로 고침 | 다음 시작에 기본값으로 다시 씀 |
| `<홈>/USER.md` | RULES.md 의 `USER.xxx` 조건이 읽는 목록; 지금은 비어 있음 | 첫 시작에 빈 채로 만들어짐; 구획 5 가 채움 | 빈 앞머리 |

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
error, config_set, approval_pending, approval, interrupted, signal, decision, judgment, delivery,
reaction, presence`. `config_set` 은 키만 적는다. 턴 도중의 steer 는 `steer: true` 가 붙은 `user`
사건이다. `tool_call` 의 인자와 `tool_result` 의 결과는 2,000자로 자른다. `approval_pending` 은
도는 중이던 메시지를 통째로 싣는다(재개에 필요). `signal` 사건의 `trust` 는 감각 자신의 선언
(`ledger.TRUST`)이지 데이터가 주장하는 값이 아니다. `decision` 은 `signal_key, action, why` 를
싣고, 알림까지 이어진 `speak` 에 한해 `notice_id, text, signal_keys` 도 싣는다(재시작 뒤
`Outbox` 가 이것으로 복구한다).

## 3. 파생 상태 (`pathfinder/state.py`)

`apply(event)` 가 유일한 투영이고 실시간 쓰기와 재구축이 같은 함수를 지난다. 멱등이며
(`seq <= cursor` 무시) `BEGIN IMMEDIATE` 와 잠금 아래 있어 프로세스 둘이 한 턴을 두 번 세지
못한다. `gated` 로 끝난 턴은 교환 횟수에 들지 않는다.

**말한 것만 이력이 된다:** `user` 사건은 `origin == "user"` 일 때(steer 포함), `assistant` 사건은
`final` 일 때만. 표 `approvals(id, call_id, session, turn, tool, args_json, messages_json, ts,
decision)`: `approval_pending` 이 넣고 `approval` 이 결정을 적는다. `pending()` · `approval(id)` 로
읽는다. `catch_up(ledger)` 는 데몬이 뜰 때, `rebuild(ledger)` 는 표를 버리고 다시 흘린다 —
승인 대기도 함께 돌아온다.

구획 3 이 더한 것: `signals(key, ts, sense, kind)`(24시간 중복 제거: `seen(key, since)`),
`decisions(signal_key, ts, action, why, notice_id, session, text, signal_keys)`, `judgments(ts)`,
`deliveries(id, ts, session, via)`, `reactions(ts, delivery_id, vote)`, `presence(session, ts,
idle_s)`, `signal_turns(ts)`(신호 기원 `turn_start` 한 번마다 한 줄 — 속도 제한용). `today(now)`
(`clock.day_start` 기준)는 `rules.decide` 가 필요로 하는 계수들을 준다: `spoken`, `judgments`,
`last_signal_ts`, `burst`(최근 60초 안의 신호 턴 수), `kind_counts`(오늘 종류별 판정 수,
`count_over` 용). `undelivered()` 는 `delivery` 가 아직 없는 `decision speak` 를 찾는다 —
`deliver.Outbox.rebuild()` 가 죽었다 살아날 때 복구하는 것. `rebuild(ledger)` 는 이 표들도
같이 버리고 다시 흘린다.

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
sandboxed, reversible, normalize)`; `schema()` 는 OpenAI 툴 dict; `coerce(spec, args)` 는 선언된
인자를 JSON 스키마 타입으로 캐스팅하고(문자열 `"false"` 가 참으로 통과해 escalate 가 되면 안
된다), 선언 안 된 키를 버리고, 빠진 필수 키를 오류로 낸다. `normalize` 는 coerce 뒤 · policy 앞에
돌아 **게이트에 보이는 것이 실제로 일어날 일**이 되게 한다: `read_file` · `write_file` 의
상대경로는 `<workspace>/경로` 가 되고 `~` 는 펼쳐진다; `"format": "local_datetime"` 을 선언한
인자는 `normalize_local_datetime(*keys)` 가 `clock.parse_local` 로 정규화하고, 그 `ValueError`
(`Z` 나 오프셋)는 모델이 손볼 수 있는 툴 오류가 된다. 핵심 툴: `run_command(command,
escalate)`(write, sandboxed) · `read_file(path)` · `write_file(path, content)`(write) — 파일 툴
둘은 `exec.SECRET_PATHS` 를 거부한다. 감각이 툴을 더 얹을 수 있다(schedule 의
`add_schedule`/`list_schedules`/`remove_schedule` — §8), `signals.load` 가 이 같은 레지스트리에
합친다.

**`policy.py`** — `decide(spec, origin, args, grants) -> allow | ask | deny`. 모델 없음, 셸 문법
해석 없음, 읽는 인자는 `escalate` 하나. 샌드박스 툴은 `sandboxed` 로 판정하되 escalate 하거나
이 기계에 샌드박스가 없으면 `write` 다; `reversible=True` 를 선언한 보통 write 는 `write` 대신
`reversible` 로 판정된다(D22).

| 출처 \ 툴 | read | sandboxed | network | write | reversible |
|---|---|---|---|---|---|
| `user` | allow | allow | allow | **ask** | **allow** |
| `signal` | allow | ask | ask | ask | ask |
| `job` | allow | ask | ask | ask | ask |
| `system` | allow | deny | deny | deny | deny |

`config.json` 의 `grants`(`{툴: allow|ask|deny}`) 는 `user` 턴에서만 표를 이기고, 상자를 벗어난
샌드박스 툴(escalate, 샌드박스 없음)에는 절대 적용되지 않으며, 세 낱말 중 하나일 때만 먹는다 —
config 가 다른 값을 거부하고 policy 도 무시한다. 오타가 게이트를 끄면 안 된다. `DENIED_TEXT` 는
실패가 아니라 정책이라고 말해 모델이 우회로를 찾지 않게 한다. `reversible` 은 자기가 만든 줄을
지워 되돌릴 수 있는 write(`add_schedule`, `remove_schedule`)를 뜻한다 — 사용자에게는 그 자체로
돌지만, 신호가 연 턴(그런 줄을 심으려는 메일)은 다른 write 와 똑같이 여전히 묻는다.

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
  `approve {id}` · `deny {id}` · `signal {sense, fields}`(push 문 — 감각이 로드되어 있고
  push 가능해야 한다, 즉 `build(fields)` 를 내보내야 한다) · `presence {idle_s}` ·
  `react {vote, id?}`. 나가는 것 `attached` · `turn.start` · `delta` · `tool {name, ok}` ·
  `gate {id, tool, args}` · `turn.end {kind}` · `notice {id, text, why}` · `error`. 깨진 줄 ·
  모르는 종류 · 없거나 이미 결정된 승인 id · 도는 턴이 없는 interrupt 는 `error` 한 줄이고
  연결은 안 끊는다.
- 세션 = 큐 + 워커 하나(`_Lane`, deque 로 만든 큐 — 보통 `asyncio.Queue` 에는 미리보기가 없다).
  큐 항목: `("say", text)` · `("resume", approval_id)` · `("continue", steer_text)` ·
  `("signal", Signal)`. `attach` 는 `session_resume_s` 안의 최근 세션을 잇는다.
- **상주 시스템 프롬프트는 `prompts/system.txt` + `signals.SIGNAL_TURNS_CLAUSE` 이고, 유저 턴과
  신호 턴을 가리지 않고 모든 턴에서 바이트 단위로 같다**(캐시); 두 번째 시스템 메시지
  `clock.now_line()`("Local time now: ...")만 턴마다 바뀐다.
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

## 8. 신호 · 규칙 · 전달 · 감각 (구획 3)

신호가 흐르는 길, 처음부터 끝까지:

```
sense.poll(options, now) -> [Signal]           (schedule.py, 30초마다)
sense.build(fields, now) -> Signal             (shell.py, 훅이 `signal` 로 push)
        │
        ▼
Daemon.push_signal ── "signal" 기록 ── state.seen(key, 24h)? ── 예: "decision silence, seen"
        │ 아니오                                                    (버려짐, 큐에 안 들어감)
        ▼
세션 lane: ("signal", Signal) — 바로 뒤에 연달아 있는 것들이 한 다발로 모인다
        │
        ▼
Daemon.run_signal_turn ── signals.hold_for (속도 제한) ── 신호마다 rules.decide:
        defer / silence ──▶ 적히고 끝, 공짜
        ask_model ──▶ judgment_cap? defer : rules.judge (스텁) ──▶ "judgment" 적고 다시 결정
        speak (전부) ──▶ Daemon._signal_model_turn
                │
                ▼
        모델 턴 하나: origin "signal", Broker(origin="signal"), 이력 없음, budget 4,
        messages = system_prompt + now_line + signals.render_envelope(다발)
        전달할 글 = 감각의 요약들 + 모델의 한마디 (LLMError/빈 답/게이트면 요약만) ──▶
        "decision speak" 로 적힘(이번엔 notice_id/text 를 담고) ──▶
        deliver.Outbox.enqueue(Notice)
                │
                ▼
        대상 세션의 턴이 도는 중이거나 presence 가 "타이핑 중"이면 들고 있는다; 모든 turn.end 와
        presence 갱신 뒤 풀린다 ──▶ 붙어 있으면 소켓 `notice`, 없으면 macOS 배너 ──▶
        "delivery" 와 "assistant {origin: signal, final: true}" 로 적힘(이력에 남아 나중에
        `pathfinder listen` 이 봄)
```

- **`clock.py`** — `ledger.py` 말고 날짜를 계산해도 되는 유일한 모듈(`test_invariants` 는 여기에
  천장을 안 두고, 다른 곳은 전부 0). `now_line()`, `local_day(ts)`, `day_start(ts)`, `hour(ts)`,
  `parse_local(text) -> (ts, canonical)`(`Z` 나 오프셋을 거부), `format_local(ts)`,
  `minutes_until`, `next_repeat`. 로컬 시각은 항상 오프셋 없는 벽시계 문자열이고, `time.mktime`
  이 다른 모든 타임스탬프와 같은 epoch 로 바꾼다.
- **`signals.py`** — `Signal(sense, kind, key, summary, fields, trust)`; `SIGNAL_ENVELOPE` 와
  `SIGNAL_TURNS_CLAUSE` 는 `bench/m3_injection.py` 에서 그대로 복사했다(m3, HISTORY.md: 바로 이
  문구가 모델이 신호 안에 숨은 지시를 따르지 않고 데이터로만 읽게 한다 — 주입 0/20, 대조군
  5/5). `render_envelope(signals)` 는 봉투 뒤에 신호마다 `render_block` 하나씩
  (kind/trust/key/summary/fields-as-JSON) — 블록의 `trust` 는 감각 자신의 선언이 쓰는 것이지
  데이터가 아니다. `load(cfg, home)` 은 설정된 `pathfinder.senses.<name>` 을 하나씩 임포트해서
  `OPTIONS`, 올바른 `TRUST`, 모든 줄이 `silence` 인 `KINDS`(침묵할 길이 없는 감각은 거부됨)를
  갖췄는지 보고, `TOOLS` 를 공용 레지스트리에 합치고, `home` 을 옵션에 `_home` 으로 심는다.
  `SenseLoop` 는 로드된 폴링 감각마다 자기 `CADENCE_S` 로 스레드에서 돌리고, 예외를 낸 감각은
  죽이지 않고 로그만 남긴다. `seen_recently`/`hold_for` 는 state 에서 읽는 중복 제거와 속도
  제한이다.
- **`rules.py`** — Mark.5 `brain/rules.py` 를 옮겨왔다(`prepare` 는 뺐고 `model` 은 `ask_model`
  로 이름을 바꿨다). `RULES.md`: 앞머리(`quiet_hours, daily_budget, min_interval_s, burst_limit,
  judgment_cap, idle_threshold_s`)에 종류마다 `## <kind>` 표 하나, 첫 매치가 이긴다. 조건 어휘는
  닫혀 있다: 핵심 `always, count_over, minutes_until_under, minutes_over` 에 로드된 감각들이
  등록한 것을 더한 것; 로드된 감각이 없는 종류의 절은 파싱은 되지만 검사는 안 한다.
  `decide(signal, rules, user_lists, today, now)`: 조용한 시간 -> defer; 하루 예산 소진 ->
  defer(`trust: user` 신호, 예를 들어 일정은 예외); 그다음 표; 매치 없음 -> `ask_model`.
  `judge(signal, cfg, today)` 는 스텁이고(D13 의 모델 호출은 구획 3의 다음 단계에 온다)
  `(False, "not implemented")` 를 돌려준다 — 데몬이 이미 그 자리를 부르고 있는 이음매다.
- **`deliver.py`** — `Notice(id, session, text, why, signal_keys)`; `Outbox.hold(session)` 은
  `is_running(session)` 이거나, 신선한(60초 이내) `presence` 줄이 `idle_s < idle_threshold_s`
  라고 말하는 동안 참이다. `release()` 는 풀 수 있는 알림을 FIFO 순서로 다 보내고, 아직 걸린
  첫 알림에서 멈춘다. `_deliver` 는 `delivery {id, via}` 와 `assistant {origin: signal, final:
  true}` 를 적고, 붙은 클라이언트가 있으면 소켓 `notice` 를, 없으면 `banner` 를 부른다(기본은
  macOS `osascript` 알림 — 테스트를 위해 갈아 끼울 수 있다). `rebuild()` 는 `state.undelivered()`
  에서 큐를 복구한다; 여기서 곧바로 흘려보내지는 않고 다음 자연스러운 `release()` 계기를
  기다린다 — 재시작이 아무 일도 일어나기 전에 느닷없이 배너를 띄우는 일이 없도록.
- **`senses/`** — 파일마다 150줄 천장. `schedule.py`: `cfg["schedules"]` 를 읽고(폴링, 30초),
  때가 된 줄을 한 번만 울리고, 반복이면 `config.set` 으로 `at` 을 미루고, 1회성이면 줄을 지운다;
  `add_schedule`/`list_schedules`/`remove_schedule` 을 내놓는다(첫째와 셋째는 `write=True,
  reversible=True`). `shell.py`: push 전용(`build(fields, now)`), `command_failed`(exit != 0) /
  `command_finished`(exit == 0), `exit_nonzero` 와 `duration_over` 를 등록한다; 중복 제거 열쇠에
  도착 시각이 들어가는데, 반복 자체가 `count_over` 가 보려는 것이기 때문이다. `docs/senses.md`
  에 zsh 훅(백그라운드로 돌리고 오류를 삼킴 — 훅이 셸을 막으면 안 된다)과 다음 단계가 더할
  Claude Code 훅의 자리표시가 있다.
- **`config.py`** 이 더한 것: `senses`(`{name: options}` — 키가 있으면 켜짐; `_check` 가 그
  감각 모듈을 임포트해 옵션을 `OPTIONS` 로 검사)와 `schedules`(`{id, at, repeat, text}` 목록;
  `at` 은 이미 `clock.parse_local` 의 표준 문자열이어야 한다).

## 9. 불변조건과 그것을 붙드는 테스트

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
| `signals.SIGNAL_ENVELOPE`/`SIGNAL_TURNS_CLAUSE` 가 그것을 잰 bench 와 바이트 단위로 같음(m3); 150줄을 넘는 감각 파일 없음; `rules.py` 는 `judge` 밖에서 llm 모듈을 임포트하지 않음 | `test_invariants.SignalEnvelopeInvariants`, `test_sense_files_under_150_lines`, `test_rules_imports_no_llm_at_top_level` |
| RULES.md 의 조건 어휘는 절마다 닫혀 있고 검사됨; 첫 매치가 이김; 전역 게이트(조용한 시간 · 예산)가 표보다 먼저 돎; 매치 없음은 `ask_model` | `test_rules.*` |
| 신호는 24시간 중복 제거됨; 몰림은 다발 전체를 들고 있음; 기본 침묵 `KINDS` 줄이 없는 감각은 거부됨; 모르는 감각이나 옵션은 `config.set` 이 거부함 | `test_signals.*`, `test_config.test_sense_options_checked_by_config` |
| 알림은 세션의 턴이 도는 중이거나 presence 가 "타이핑 중"이면 들려 있다가 `turn.end`/`presence` 뒤 순서대로 풀리고, 클라이언트가 없으면 배너를 띄우고, 장부에 아직 `delivery` 가 없는 `decision speak` 줄에서 복구됨 | `test_daemon.DeliverTest.*` |
| 신호 턴의 origin 은 `signal`(write 는 게이트에 걸려 절대 안 돎); 봉투는 `state.history` 에 안 남고 전달된 글은 남음; 막힌 모델도 감각의 요약은 전달함 | `test_daemon.SignalTurnTest.*` |
| 상주 시스템 프롬프트(`system.txt` + Signal-turns 절)는 턴마다 바이트 단위로 같음; 로컬 시각 줄은 따로 떨어진 시스템 메시지 | `test_daemon.ResidentPromptTest.test_resident_prompt_frozen` |
| D22: reversible write 는 사용자에게 그 자체로 돌고, signal/job 에는 여전히 묻고, system 에는 여전히 거부됨; 되돌릴 수 없는 write 는 그대로임 | `test_policy.test_policy_reversible` |
| `local_datetime` 은 `Z`/오프셋을 거부하고 표준화함; 일정은 한 번만 울리고 반복은 미뤄짐; 같은 종류의 실패 세 번은 말하고 그보다 적으면 침묵함 | `test_tools.test_local_datetime_normalize`, `test_senses.*` |
| `state.db` 삭제: `seen()` 과 `undelivered()` 가 장부만으로 돌아옴 | `test_daemon.ReindexRestoresSignalsTest.test_reindex_restores_seen_and_undelivered` |

## 10. 세는 법

```bash
python3 -m unittest discover -s tests            # 전체 테스트, 네트워크 없음 (123)
wc -l pathfinder/*.py pathfinder/senses/*.py     # 코어; 배분: ledger 400 · state 600 · daemon 600 ·
                                                  # loop 400 · broker+policy 350 · exec 300 · tools 300 ·
                                                  # clock+signals+rules 450 · deliver 250 · 감각 파일 150
python3 scripts/scan_personal.py --all           # 추적 파일 전체 개인정보 스캔
```

## 11. 돌리는 법

```bash
python3 -m pathfinder config set model provider:model-name
python3 -m pathfinder daemon
python3 -m pathfinder say "안녕"                  # 답이 흐르는 중의 두 번째 `say` 는 steer
python3 -m pathfinder listen                      # 붙어서 델타와 신호 알림을 출력
python3 -m pathfinder approvals                   # 승인 대기 목록, state.db 에서 읽음
python3 -m pathfinder approve ID | deny ID        # 게이트에 걸린 턴을 재개하고 답을 출력
python3 -m pathfinder interrupt                   # 최근 세션의 도는 턴을 멈춤
python3 -m pathfinder config set grants '{"write_file": "allow"}'
python3 -m pathfinder config set senses '{"schedule": {}, "shell": {}}'
python3 -m pathfinder signal shell '{"cmd": "make test", "exit": 1, "seconds": 3, "cwd": "/tmp"}'
python3 -m pathfinder react up                    # 최근 전달에 엄지척
python3 -m pathfinder rules                       # RULES.md 의 경로를 출력
python3 -m pathfinder reindex                     # 데몬이 도는 중에는 거부
```

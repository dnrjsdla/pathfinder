# ARCHITECTURE (한국어본)

**지금 코드가 하는 일만 적는다.** 왜 이렇게 생겼는지는 `docs/blueprint/` 와
`docs/decisions/DECISIONS.md`, 실측이 바꾼 것은 `HISTORY.md`. 여기 적힌 이름은 전부 트리에
실존한다. 정본은 영어 `ARCHITECTURE.md` 이고 이 파일은 같은 커밋에서 함께 고친다.

지금까지 지은 것: **구획 1 — 최소 왕복.** 툴 · 신호 · 작업자 · 기억 · 설치기는 아직 없다.

## 0. 한 문장

모든 사실은 먼저 장부 하나에 적히고, 나머지는 전부 거기서 파생되며 지워도 된다.

```
클라이언트 (nc -U, `pathfinder say`)
   │  <홈>/pathfinder.sock 위의 NDJSON  (0600, TCP 없음)
   ▼
daemon.Daemon ── 세션마다 lane 하나 (큐 + 워커)
   │  record(): event = ledger.append(...)  →  state.apply(event)
   ├─▶ ledger.Ledger   <홈>/events/<UTC 날짜>.jsonl   append-only, 진실
   ├─▶ state.State     <홈>/state.db                  파생, 다시 만들 수 있음
   └─▶ loop.run(step, messages, call_tool, emit)      워커 스레드에서
            └─ step = llm.openai_compat.stream(endpoint, messages)   SSE
```

## 1. 무엇이 어디에 사나

| 자리 | 무엇 | 쓰는 길 | 지우면 |
|---|---|---|---|
| `<홈>/events/<날짜>.jsonl` | 사건 전부, 한 줄에 JSON 하나 | `ledger.Ledger.append()` 하나뿐 | 기록을 잃는다 — 유일한 진실 |
| `<홈>/state.db` | 세션 · 메시지 · 커서 | `state.State.apply()` | `pathfinder reindex`, 또는 그냥 재시작 — 데몬이 따라잡는다 |
| `<홈>/config.json` | 모델 이름 · provider · 손잡이 | `config.set()` 하나뿐 | 기본값(모델 미설정) |
| `<홈>/pathfinder.sock` | 데몬의 소켓 | 데몬 | 뜰 때 다시 만든다 |

`<홈>` 은 `$PATHFINDER_HOME` 또는 `~/.pathfinder` (권한 0700).

## 2. 장부 (`pathfinder/ledger.py`)

- `partition(ts)` 가 시각을 UTC 날짜로 바꾼다. **시각이 파일 이름이 되는 자리는 여기 하나뿐이다.**
  읽는 쪽은 날짜를 계산하지 않는다 — 파티션을 순서대로 훑고 `seq` 로 거른다.
- 사건은 예약 칸 `seq, ts, type, session, turn, origin, trust` 와 데이터로 이뤄진다. `seq` 는
  전역이고 엄격히 증가한다. `type` · `origin` · `trust` 는 닫힌 집합이고 그 밖의 값은
  `append()` 에서 거부된다. 어휘를 버린다는 것은 집합에서 지운다는 뜻이고, 그 뒤로 장부가
  그 낱말을 받지 않는다.
- 프로세스 둘이 쓸 수 있다(데몬, 그리고 `config set` 을 도는 CLI). 그래서 append 마다 스레드
  잠금과 `events/.lock` 의 배타 `flock` 을 잡고, 최신 파티션의 크기가 내가 모르는 사이
  바뀌었으면 파일 꼬리에서 마지막 `seq` 를 다시 구한다.
- 죽었다 살아날 때: 마지막 `seq` 는 파티션을 **최신부터 거꾸로** 훑어 온전한 줄이 나올 때까지
  찾는다. 최신 파일 하나만 믿으면 안 된다 — UTC 자정 직후 첫 쓰기에서 죽으면 그 파일은 비었거나
  잘린 줄 하나뿐이고, 그러면 `seq` 가 1 부터 다시 발급되어 상태가 새 사건을 전부 "이미 본
  것"으로 조용히 버린다. 개행 없이 끝난 조각 뒤에는 개행을 먼저 넣고 쓴다 — 새 사건이 조각에
  들러붙어 함께 사라지지 않게. `read()` 는 못 읽는 줄을 건너뛴다.
- 알려진 천장: `fsync` 없이 `flush()` 만 한다 — OS 가 죽으면 마지막 줄을 잃을 수 있다.

지금의 사건 종류: `session_start, user, assistant, tool_call, tool_result, turn_start, turn_end,
error, config_set`. `config_set` 은 키만 적고 값은 적지 않는다.

## 3. 파생 상태 (`pathfinder/state.py`)

`apply(event)` 가 유일한 투영이고, 실시간 쓰기와 재구축이 같은 함수를 지나므로 둘이 갈릴 수
없다. 멱등이다(`seq <= cursor` 는 무시) — 커서를 `BEGIN IMMEDIATE` 안에서 읽고 같은
트랜잭션에서 전진시키며 잠금 하나를 잡으므로, 두 번째 프로세스나 워커 스레드가 같은 턴을 두
번 세지 못한다.

**말한 것만 이력이 된다:** `user` 사건은 `origin == "user"` 일 때, `assistant` 사건은 `final`
일 때만 센다. 신호 봉투와 턴 도중의 잡음은 `history()` 에 닿지 않는다. `catch_up(ledger)` 는
데몬이 뜰 때 돌고, `rebuild(ledger)` 는 표를 버리고 처음부터 다시 흘린다.

## 4. 모델 층 (`pathfinder/llm/`)

- `resolve(cfg, "provider:model")` 은 첫 콜론에서 갈라 `Endpoint` 를 돌려준다. 모델 이름은
  `config.json` 에만 있고 기본값은 비어 있다. 미설정이면 분명한 오류가 난다.
- `openai_compat.stream()` 은 OpenAI 호환 chat-completions SSE 를 말하고
  `("text" | "reasoning" | "tool_calls" | "usage" | "cancelled", payload)` 를 낸다. 툴 호출은
  index 가 붙은 조각들에서 조립한다. 인자는 문자열 조각으로도 객체로도 올 수 있고, 빈 id 는
  대체 id 를 받는다. 실패는 전부 `LLMError` 다 — HTTP 상태, 연결 오류, 그리고 스트림 도중에
  오는 것들(읽기 타임아웃 · 리셋 · 덜 온 응답). 402 · 429 면 `blocked` 가 참이다.

## 5. 루프 (`pathfinder/loop.py`)

`run(step, messages, call_tool, emit, budget)` 이 한 턴을 끝까지 돌리고
`Outcome("answer" | "exhausted" | "cancelled")` 로 끝난다. 루프는 모델 이름도 출처도 정책도
모른다. reasoning 은 이력에 되돌려 넣지 않고, assistant 메시지는 `role / content / tool_calls`
로 다시 짓는다. 예산은 비용 상한이지 종료 조건이 아니다. 어떤 결말이든 그 턴에 흘려보낸
텍스트를 전부 싣는다 — 예산이 바닥나거나 취소된 턴도 이력에 남는다. 예외는 호출부로 올라간다.

## 6. 데몬 (`pathfinder/daemon.py`)

- 규약: 한 줄에 JSON 객체 하나. 들어오는 것 `attach {session?}` · `say {text}`. 나가는 것
  `attached` · `turn.start` · `delta` · `tool` · `turn.end {kind}` · `error`. 깨진 줄이나 모르는
  종류에는 `error` 를 돌려주고 **연결은 안 끊는다.**
- 세션 = 큐 + 워커 하나. 한 번에 한 턴이고 두 번째 `say` 는 기다린다. `attach` 는
  `session_resume_s` 안에 쓰인 가장 최근 세션을 잇고, 없으면 새로 연다.
- 한 턴: `user` → `turn_start` → 시스템 프롬프트 + `state.history()` → 스레드에서 `loop.run` →
  `assistant {final}` → `turn_end {kind, steps, usage}`. 워커 스레드는 출력을 큐 하나로
  돌려주고 끝 표시도 자기가 넣는다 — 그래서 `turn.end` 뒤에 delta 가 올 수 없다.
- 장부와 상태에 쓰는 일은 전부 이벤트 루프 스레드에서 한다(`record()`).
- 턴마다 `threading.Event` 하나(`Session.cancel`)를 가지며 step factory 를 거쳐 HTTP
  스트림까지 닿는다. 아직 아무도 세우지 않는다 — interrupt 와 steer(구획 2)가 이 위에 선다.
- **모델이 막혀도 데몬은 산다:** `LLMError`(크레딧 소진 · 한도 · 오프라인 · 모델 미설정)는
  `error` 사건, `error` 줄, `turn.end {kind: "error"}` 가 된다.
- 소켓은 umask 0177 아래에서 만들어져 0600 으로 태어난다. 죽은 소켓 파일은 지우고, 살아
  있는 소켓이면 거부한다. `close()` 는 붙어 있는 클라이언트부터 닫는다.

## 7. 불변조건과 그것을 붙드는 테스트

| 불변조건 | 테스트 |
|---|---|
| 장부에 쓰는 길 하나 · 파티션 함수 하나 · 설정에 쓰는 길 하나 | `test_invariants.test_single_writer` |
| 닫힌 어휘 | `test_ledger.test_ledger_closed_vocab` |
| 13일에 걸친 세션이 통째로 읽힌다 | `test_ledger.test_partition_roundtrip` |
| 두 프로세스가 같은 `seq` 를 내지 않는다 | `test_ledger.test_two_ledgers_never_duplicate_seq` |
| 상태는 다시 만들 수 있고 apply 는 멱등이다 | `test_state.test_reindex_restores_history` |
| 말한 것만 기억한다 | `test_state.test_restore_said_only` |
| `state.db` 를 지워도 대화가 산다 | `test_daemon.test_daemon_resumes_after_state_deleted` |
| 모델이 막히거나 미설정이어도 데몬은 산다 | `test_daemon.test_daemon_survives_model_402`, `test_unset_model_is_an_error_not_a_crash` |
| 최신 파티션이 비었거나 잘려도 `seq` 가 산다 · 조각 뒤에 쓴 사건이 읽힌다 | `test_ledger.CrashRecoveryTest` |
| 턴은 한 번만 세어진다, 프로세스가 둘이어도 | `test_state.test_two_states_on_one_db_count_each_turn_once` |
| 예산이 바닥나도 흘려보낸 말은 이력에 남는다 | `test_daemon.test_exhausted_turn_still_records_what_was_said` |
| 스트림 도중의 실패도 `LLMError` 다 | `test_llm.test_mid_stream_failures_become_llm_errors` |
| `ledger.partition()` 밖에서는 날짜를 계산하지 않는다 | `test_invariants.test_single_writer` (패키지 전체를 훑는다) |
| 깨진 줄이 연결을 끊지 않는다 | `test_daemon.test_daemon_unknown_line_keeps_connection` |
| 소켓 0600 · 유닉스 소켓만 · 소스에 모델 ID 없음 | `test_daemon.test_socket_perms`, `test_invariants.test_no_model_ids_or_tcp_in_source` |
| reasoning 을 되돌려 보내지 않는다 | `test_loop.test_loop_does_not_echo_reasoning` |

## 8. 세는 법

```bash
python3 -m unittest discover -s tests            # 전체 테스트, 네트워크 없음
wc -l pathfinder/ledger.py pathfinder/state.py pathfinder/daemon.py pathfinder/loop.py pathfinder/config.py
python3 scripts/scan_personal.py --all           # 추적 파일 전체 개인정보 스캔
```

코어 배분(합의 문서 §6): ledger 400 · state 600 · daemon + lane 600 · loop 400. 자기 배분의
1.3배를 넘는 모듈은 그 구획이 끝날 때 보고한다.

## 9. 돌리는 법

```bash
python3 -m pathfinder config set model provider:model-name
python3 -m pathfinder daemon
python3 -m pathfinder say "안녕"
python3 -m pathfinder reindex        # 데몬이 도는 중에는 거부한다 — 재시작하면 알아서 따라잡는다
```

`config.json` 의 값은 두 문(`config set` 과 손으로 고친 파일) 모두에서 타입을 검사한다.
`max_steps` 는 1 이상이어야 한다. 홈 디렉터리는 이미 있던 것이라도 0700 으로 강제한다.

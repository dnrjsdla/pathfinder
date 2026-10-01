# ARCHITECTURE (한국어본)

**지금 코드가 하는 일만 적는다.** 왜 이렇게 생겼는지는 `docs/blueprint/` 와
`docs/decisions/DECISIONS.md`, 실측이 바꾼 것은 `HISTORY.md`. 여기 적힌 이름은 전부 트리에
실존한다. 정본은 영어 `ARCHITECTURE.md` 이고 이 파일은 같은 커밋에서 함께 고친다.

지금까지 지은 것: **구획 5 — 기억.** 비서가 말하고(1) 툴을 부르고(2) 세상이 말하고(3) 작업이
제 턴과 별도로 돌고(4); 이제 `memory.Manager` 만 쓰는 기억 볼트 - `USER.md`, `RULES.md`,
`wiki/*.md` - 를 지닌다. 모델 없는 문(`vet`)을 지나야 어떤 수정이든 들어간다. wiki 수정은 그
자리에서 적용되고, `USER.md`/`RULES.md` 수정은 툴 게이트와 같은 승인 상자로 간다(D28: 사용자
턴의 grant 가 그 자리 적용을 허락할 수도 있다). 통합 패스(`Manager.run_pass`)는 작업자처럼 사용자
자신의 턴과 별도로 돌며 지난 패스 이후의 장부를 읽어 남길 만한 것을 적는다 - wiki 한 줄, 사용자가
제 말로 한 사실, 또는(되풀이된 판정 뒤) `RULES.md` 줄 제안 - 그리고 오래된 세션을 요약 하나로
접는다. `USER.md` 의 본문과 wiki 지도는 모든 턴의 두 번째 시스템 메시지에 실려 오고,
`search_memory`/`read_memory` 가 모델이 나머지를 찾아보게 한다. `RULES.md` 는 이제 재시작 없이
다시 읽힌다 - 승인된 수정에서도, 손으로 고쳤을 때도. 카탈로그와 TUI 는 그 뒤 구획들이다.

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
| `<홈>/config.json` | 모델 이름 · provider · `grants` · `senses` · `schedules` · `mcp_servers` · 손잡이 | `config.set()` 하나뿐(설치기도 이 문으로) | 기본값(모델 미설정) |
| `<홈>/workspace/` | 샌드박스 셸이 쓸 수 있는 유일한 곳 · 모든 셸 명령의 cwd · 상대경로 파일이 놓이는 곳 | 사용자의 요청으로 툴이 | PATHFINDER 의 것은 없음 |
| `<홈>/pathfinder.sock` | 데몬의 소켓 | 데몬 | 뜰 때 다시 만든다 |
| `<홈>/RULES.md` | speak/silence/defer/ask_model 규칙, 사람이 읽을 수 있는 한 장 | 첫 시작에 `pathfinder/prompts/RULES.md` 에서 만들어짐; 손으로 고치거나 승인된 `edit_memory` 줄로; 어느 쪽이든 재시작 없이 다시 읽힘 | 다음 시작에 기본값으로 다시 씀 |
| `<홈>/USER.md` | RULES.md 의 `USER.xxx` 조건이 읽는 목록에 자유 본문(상주 문맥)이 더해짐 | 첫 시작에 빈 채로 만들어짐; 승인된 `edit_memory` 줄이 채움(또는 사용자 턴 grant 의 자동 적용, D28) | 빈 앞머리 - 기억이 처음부터 다시 시작 |
| `<홈>/wiki/*.md` | wiki 페이지, 파일 하나에 주제 하나 | `memory.Manager.apply`(`edit_memory`, 그 자리에서 적용) | 그 지식이 사라짐 - 파일이 진실이다 |
| `~/.claude.json`(사용자의 것, `<홈>` 아님) | Claude Code 의 user scope `mcpServers`(D33) - 승인된 설치마다 항목 하나, 비밀은 `${VAR}` 참조 | `installer.Installer` 만, 원자적으로, `~/.claude.json.pathfinder-<ts>` 로 바이트 복사한 뒤 | Claude Code 의 몫; 설치기는 롤백·제거 때 백업에서 되돌린다 |
| `~/Library/LaunchAgents/pathfinder.daemon.plist` | launchd 에이전트: 지금 인터프리터로 `-m pathfinder daemon`, 리터럴 `PATH`, 설정돼 있으면 `PATHFINDER_HOME`, `KeepAlive`, 로그는 `<home>/daemon.log`, `WorkingDirectory` = 패키지의 부모 | `init`(plistlib), 예라고 한 뒤 | `init` 을 다시 돌리기 전까지 `pathfinder service start` 가 실패 |
| `<home>/daemon.log` | launchd 아래 데몬의 stderr | launchd | 없음 |
| `<home>/hooks/pathfinder.zsh` 와 `~/.zshrc` 의 가드된 한 줄 | 셸 감각의 `preexec`/`precmd` 훅(`docs/senses.md`), 파일이 있을 때만 source | `init`, 예라고 한 뒤; `~/.zshrc` 는 `~/.zshrc.pathfinder-<ts>` 로 백업하고 덧붙이기만, 다시 쓰지 않음 | 셸 감각이 아무것도 못 듣는다 |
| `~/.claude/settings.json`(`hooks.Stop`, `hooks.Notification`) | Claude Code 감각의 훅 두 항목 | `init` 이 `config.merge_json_file`(D60)로: 백업 · 원자 쓰기 · 읽어서 대조; `pathfinder signal claude_code` 표식으로 멱등 | Claude Code 자기 일 |
| macOS Keychain `pathfinder-mcp-<id>` / `pathfinder-mail` | 모든 비밀(MCP 서버 토큰, IMAP 비밀번호) | 주인이 가려서: 화면의 `/secret <id> NAME` 나 `/secret mail`, 셸의 `pathfinder secret ...`(둘 다 `keychain.target` 이 푼다), 또는 데몬의 `secret.ask` 에 대한 답(`installer.secret`); 메일 감각이 켜져 있고 항목이 없으면 데몬은 화면이 붙을 때 묻는다 | 서버는 `disabled: secret missing: <NAME>` 로 남고, 메일 감각은 오류 |
| macOS Keychain `pathfinder-pasted` / `pasted-<seq>[-i]` | 주인이 입력한(`trust: user` 인 사건) 토큰 모양 문자열(`keychain.TOKEN_RE`), 이름은 그 사건의 원장 seq; 사건에는 그 자리에 `[secret:pasted-<seq>]` 이 남고(D53, D66), 그 세션의 화면과 `say` 는 핸들과 묶는 법을 알리는 `pasted` 줄을 받는다. 다른 사건에서는 그 문자열이 키체인 호출 없이 `[redacted]` 로 쓰인다 | `ledger.append()` 가 `keychain.vault` 로, 같은 이름의 항목(다른 홈의 것)이 이미 있으면 덮어쓰지 않는다; 만든 지 24시간이 지나면 다음 붙여넣기나 데몬 시작 때 삭제; `keychain.bind`(셸이나 화면의 `secret <id> NAME --from pasted-<seq>`)가 `pathfinder-mcp-<id>`/NAME 으로 옮기고 지운다 - 만료됐거나 모르는 핸들이면 `expired - enter it again with /secret` | 원장에는 핸들만 남고 값은 사라진다 - `/secret` 으로 다시 입력 |
| `<홈>/pasted.json` (0600) | `{"pasted": {핸들: 만든 시각}}` - 값은 절대 없음; 청소 때마다 항목이 사라진 핸들도 지운다 | 원장 잠금 아래의 `keychain.vault`, 데몬 시작 때의 `keychain.sweep`, 그리고 옮긴 핸들을 지우는 `keychain.bind` | 빈 것으로 읽힌다: 같은 번호를 두 번 주는 일은 없지만(seq 는 다시 나오지 않는다) 목록에 있던 항목은 더는 청소되지 않고 키체인에 모르는 채로 남는다 |

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
reaction, presence, memory_pass, memory_apply, session_summary`. `config_set` 은 키만 적는다. 턴
도중의 steer 는 `steer: true` 가 붙은 `user` 사건이다. `tool_call` 의 인자와 `tool_result` 의
결과는 2,000자로 자른다. `approval_pending` 은 도는 중이던 메시지를 통째로 싣는다(재개에 필요) -
`edit_memory` 제안이면 빈 목록이다(게이트 걸린 턴이 아니라 상자다). `signal` 사건의 `trust` 는
감각 자신의 선언(`ledger.TRUST`)이지 데이터가 주장하는 값이 아니다. `decision` 은 `signal_key,
action, why` 를 싣고, 알림까지 이어진 `speak` 에 한해 `notice_id, text, signal_keys` 도 싣는다
(재시작 뒤 `Outbox` 가 이것으로 복구한다). `memory_pass {since, upto, read, steps, usage, applied,
proposed}` 는 통합 패스마다 한 번 적힌다(`upto` 가 `state.memory_cursor` 가 됨; D39: 모델이 끝내 답하지 못한 패스는 `upto = since` 로 적는다);
`memory_apply {target, op, old, new, evidence, approval?}` 는 적용된 볼트 수정 하나의 되돌리기
기록 전부다; `session_summary {upto, text}` 는 세션의 앞부분을 접는다. origin 에 `memory` 가
더해진다(패스 자신의 턴과 툴 사건).

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
idle_s)`, `signal_turns(ts)`(신호 기원 `turn_start` 한 번마다 한 줄 — 속도 제한용).
`today(now, idle_threshold_s=)` 는 `rules.decide` 가 필요로 하는 것들을 준다: `spoken`
과 `judgments`(`clock.day_start` 기준), `last_signal_ts`, `burst`(최근 60초 안의 신호 턴 수),
`recent_counts`(최근 600초 안의 그 종류 신호 수 — `decisions` 가 아니라 `signals` 에서 재는데,
신호는 자기 판정보다 먼저 적히므로 "이번 것 포함"이 저절로 나온다; `>= N` 은 N번째 자신이
말한다는 뜻), `away`(`presence` 에서 — 데몬의 신호 턴 심사가 `idle_threshold_s` 를
건넬 때만; 조건 술어는 `rules` 나 `state` 자체를 못 본다). `undelivered()` 는 `delivery` 가 아직 없는 `decision speak` 를 찾는다 —
`deliver.Outbox.rebuild()` 가 죽었다 살아날 때 복구하는 것. `rebuild(ledger)` 는 이 표들도
같이 버리고 다시 흘린다.

구획 5 가 더한 페이지 색인: `pages(path, title, mtime, text)` 와 외부-내용 FTS5 표 `pages_fts`,
`vectors(path, model, vec)`(페이지마다 float32 blob 하나, `struct.pack`, numpy 없음 - 낡은
모델의 벡터는 절대 맞지 않는다); `index_page`/`remove_page`/`pages()` 가 이를 `wiki/*.md` 와
맞춰 둔다. `search(query, query_vector=None, model, top_k=3)` 는 FTS5 접두 토큰 매치와 벡터
코사인을 reciprocal rank fusion(k=60)으로 합친다; `query_vector` 없으면 FTS 만 돈다(D10 의
대체 경로는 인자 하나 뺀 같은 함수). `summaries(session, upto, text)` 가 `history(session)` 를
뒷받침한다: 요약이 있으면 그것으로 지은 `system` 줄 뒤에 `upto` 넘는 메시지만 따른다 - 세션의
앞부분은 접히지 다시 보내지지 않는다(`test_history_folds_after_summary`). `meta.memory_cursor`
(마지막 패스의 `upto`, 처음엔 0)와 `memory_stats(now)`(최근 사흘 로컬 하루씩의 패스/읽은
줄/적용 수, 대기 중인 `edit_memory` 제안, 최근 이레 하루씩의 판정 수)가 `pathfinder memory` 와
막힌-기억 방아쇠를 뒷받침한다. `rebuild(ledger)` 는 이 표들도 같이 버리고 다시 흘린다.

## 4. 모델 층 (`pathfinder/llm/`)

- `resolve(cfg, "provider:model")` 은 첫 콜론에서 갈라 `Endpoint` 를 돌려준다. 모델 이름은
  `config.json` 에만 있고, 미설정이면 분명한 오류가 난다. `Endpoint.api` 는
  `providers.<name>.api` — 없으면 `"openai"`(기존 설정 전부 그대로 풀린다), 아니면
  `"anthropic"`; 그 밖은 설정 오류. `llm.stream(endpoint, ...)` 이 그것으로 어댑터를 고른다 —
  slot 과 `rules.judge` 는 어댑터 모듈이 아니라 그것을 부른다(D2).
- `openai_compat.stream(endpoint, messages, tools, cancel)` 은 OpenAI 호환 SSE 를 말하고
  `text | reasoning | tool_calls | usage | cancelled` 를 낸다. `cancel` 은 `is_set()` 이 있는
  무엇이든 되고 청크 사이에서 확인한다. 실패는 전부 `LLMError`, 402 · 429 는 `blocked`. 데몬의
  step factory 는 툴 스키마를 항상 넘긴다 — 모델이 자기 손을 봐야 한다(첫 라이브 실행에서
  안 넘기고 있던 것이 잡혔다 — `test_default_step_factory_sends_tool_schemas`).
- `anthropic.stream`(D37, **실험적 — 라이브로 돌린 적 없음, D7**)은 Messages API SSE
  (`/v1/messages`, `anthropic-version: 2023-06-01`, `x-api-key`)를 말하고 같은 튜플을 낸다: 루프의
  OpenAI 모양 이력을 가장자리에서 바꾼다(system 메시지 → 최상위 `system`, 툴 호출 → `tool_use`
  블록, 툴 결과 → user 턴 하나 안의 `tool_result` 블록), `input_json_delta` 조각은 이어 붙여
  `content_block_stop` 에서 파싱, 스트림 도중 `error` 이벤트는 `LLMError`. 테스트는 녹화 픽스처만
  (`tests/fixtures/anthropic_*.sse`). 임베딩 없음: `embed` 는 호환 어댑터에 남는다.
- `openai_compat.embed(endpoint, texts) -> list[list[float]]` 는 같은 `/embeddings` OpenAI 호환
  경로로 묶어서 임베딩한다. 임베더는 `cfg["embed_model"]` 이고 `model`/`worker_model` 과 같은
  방식으로 `resolve` 를 거친다; 비어 있으면 벡터 없음이지 오류가 아니다.

## 5. 툴 · 정책 · 샌드박스 · 브로커

**`tools.py`** — `ToolSpec(name, description, params, required, handler, write, network,
sandboxed, reversible, origins, normalize)`; `schema()` 는 OpenAI 툴 dict; `coerce(spec, args)` 는
선언된 인자를 JSON 스키마 타입으로 캐스팅하고(문자열 `"false"` 가 참으로 통과해 escalate 가 되면
안 된다), 선언 안 된 키를 버리고, 빠진 필수 키를 오류로 낸다. `normalize` 는 coerce 뒤 · policy
앞에 돌아 **게이트에 보이는 것이 실제로 일어날 일**이 되게 한다: `read_file` · `write_file` 의
상대경로는 `<workspace>/경로` 가 되고 `~` 는 펼쳐진다; `"format": "local_datetime"` 을 선언한
인자는 `normalize_local_datetime(*keys)` 가 `clock.parse_local` 로 정규화하고, 그 `ValueError`
(`Z` 나 오프셋)는 모델이 손볼 수 있는 툴 오류가 된다. 핵심 툴: `run_command(command,
escalate)`(write, sandboxed) · `read_file(path)` · `write_file(path, content)`(write) — 파일 툴
둘은 `exec.SECRET_PATHS` 를 거부한다. 감각이 툴을 더 얹을 수 있고(schedule 의
`add_schedule`/`list_schedules`/`remove_schedule` — §8), `jobs.py` 도 임포트되는 순간 `start_job`
을 같은 식으로 더한다(§9). `origins`(기본값: 넷 다 — `ledger.ORIGINS`)는 어느 origin 의 턴이
그 툴의 스키마를 볼 수 있는지 이름 붙인다; `for_origin(origin)` 이 `TOOLS` 를 그것으로 거른다 -
`start_job` 은 `origins={"user"}` 를 선언해서, 작업자 자신의 레지스트리와 신호 턴의 레지스트리에는
아예 없다 — "작업자는 작업자를 못 만든다"는 정책이 아니라 구조다. `origins` 는 `memory` 에
한해 옵트인이다: 넷이 기본값이라, 통합 패스는 스스로 `memory` 를 더한 툴만 본다
(`search_memory`, `read_memory`, `read_ledger`, `edit_memory`) - `write_file`, `run_command`,
schedule 의 툴은 절대 못 본다. `clip_for_ledger(value,
limit=2000)` 은 장부에 적히는 `tool_call`/`tool_result`(작업자 자신의 것도)가 갖는 머리만 자르는
경계다 — 모델이 보는 것을 자르는 `clip()`(머리+꼬리)보다 짧고 단순하다.

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
| `memory` | allow | deny | deny | deny | allow |

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

`run(step, messages, *, call_tool, emit, budget, control, resume, final_step)` 이 한 턴을
`Outcome("answer" | "exhausted" | "cancelled")` 로 끝낸다. 게이트는 결말이 아니라 예외다. 예산을
다 썼는데 마지막 응답이 여전히 툴 호출이면 `final_step`(같은 공장, 툴 없음 - 호출 자리 넷이 모두
넘긴다)이 요청을 한 번 더 보내, 소진된 턴도 말로 끝나고 다음 턴에 매달린 `user` 메시지를 남기지
않는다(D30).
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

- 규약, 한 줄에 JSON 하나. 들어오는 것 `attach {session?, cwd?}`(문자열 `cwd` 는 - 그 감각이
  켜져 있으면 - `attach` 감각의 `context` 신호를 push 한다) · `say {text}` · `interrupt` ·
  `approve {id}` · `deny {id}`(작업자 자신의 승인은 세션 lane 대신 `jobs.Manager.resume` 으로
  간다 — §9) · `signal {sense, fields}`(push 문 — 감각이 로드되어 있고 push 가능해야 한다, 즉
  `build(fields)` 를 내보내야 한다; `jobs` 는 절대 아니다) · `presence {idle_s}` ·
  `react {vote, id?}` · `job.stop {id}` · `job.steer {id, text}` · `secret {server_id, name, server,
  value}`(D53 의 주 경로: 화면이 가려 받은 답, `installer.secret` 가 받아 워커 스레드에서
  `keychain.take` 로 저장하고 기록하지 않으며, 뒤에서 그 서버를 다시 제안한다). 나가는 것 `attached` ·
  `turn.start` · `delta` · `tool {name, ok}` · `gate {id, tool, args}` · `turn.end {kind}` ·
  `notice {id, text, why}` · `job.start {id, goal}` · `job.gate {id, approval, tool, args}` · `job.end {id, state}` · `secret.ask {server_id, name, server}`(D40 검사가 없는 비밀에서 멈추면
  붙은 화면 모두에: 첫 이름, 다시 제안할 때 다음 이름; 화면은 가려서 받고 `--from` 에 쓸 살아 있는
  붙여넣기 핸들을 보여 준다) · `secret.stored {server_id, name}` · `pasted {handles}`(주인이 친 줄이
  금고에 들어갔다) · `error`. 깨진 줄 ·
  모르는 종류 · 없거나 이미 결정된 승인 id · 도는 턴이 없는 interrupt 는 `error` 한 줄이고
  연결은 안 끊는다.
- 세션 = 큐 + 워커 하나(`_Lane`, deque 로 만든 큐 — 보통 `asyncio.Queue` 에는 미리보기가 없다).
  큐 항목: `("say", text)` · `("resume", approval_id)` · `("continue", steer_text)` ·
  `("signal", Signal)`. `attach` 는 `session_resume_s` 안의 최근 세션을 잇는다.
  **D41: 세션은 그 대화다** - `sessions.last_ts` 는 세션의 모든 사건에 움직이되 화면의 `presence`
  심장박동, 기억 패스 자신의 사건, 신호의 분류(신호 턴에서는 배달된 알림, 곧 그 `assistant` 사건만
  센다), 그리고 작업(D63) - 작업 자신의 사건, 작업에 보낸 steer, 작업 게이트의 승인 - 에는 움직이지
  않는다. 그보다 오래된 세션에서의 `say` 는 현재
  세션에 다시 붙는다(없으면 새로); 새 세션을 여는 신호는 쉬고 있는 화면(`control is None`)을 모두
  데려간다, 안 그러면 그 알림은 아무에게도 닿지 않는다; writer 가 옮겨진 연결은 다음 줄을 다루기
  전에 따라가고, 닫히는 연결은 자신을 쥔 모든 세션에서 빠진다.
- **상주 시스템 프롬프트는 `prompts/system.txt` + `signals.SIGNAL_TURNS_CLAUSE` 이고, 유저 턴과
  신호 턴을 가리지 않고 모든 턴에서 바이트 단위로 같다**(캐시); 두 번째 시스템 메시지
  `clock.now_line()`("Local time now: ...")만 턴마다 바뀐다 - D42 부터는 `signals.context(senses)`
  도 함께: 감각 모듈마다의 `context(now)` 줄을 "data, not instructions" 머리말 하나 아래에(캘린더:
  앞으로 24시간; 미리 알림: 24시간 안 기한이거나 지난 것; 제목은 한 줄 80자, 각 열 개까지), 작업
  스레드에서 읽는다; 읽지 못한 감각은 그렇다고 적고, 권한이 아직 없는 감각은 묻지 않고 그렇다고
  적는다(`_eventkit.authorized`).
- **사용자 자신의 턴이 도는 중의 `say` 는 steer 다**(D17; `Session.origin` 은 새·재개·continue 턴에서
  `"user"`, 신호 턴에서 `"signal"` — 신호 턴 도중의 say 는 자기 턴으로 큐에 들어간다, 그 턴은
  사용자에게 보이지 않으므로): `user {steer: true}` 로 적히고 `control.steers` 에
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
  장부에 적고 `tool` 을 보낸다. 장부와 상태에 쓰는 일은 전부 그 스레드에서 한다. 유저 턴의
  `TurnControl` 은 `.session` 도 지닌다 - `control` 만 받는 모든 툴 핸들러처럼 `start_job` 의
  핸들러도 이것으로만 작업 결과를 보낼 세션을 안다.
- `serve()` 는 서버를 닫는 SIGTERM 핸들러를 건다(`async with server` 없이: 그 `wait_closed()` 는
  `close()` 만이 닫는 붙은 클라이언트를 영원히 기다린다); SIGTERM(launchd 의 `service stop`)과 Ctrl-C 둘 다
  `close()` 하나로 끝난다(MCP 서버 닫기, 그 프로세스 그룹 죽이기, 소켓 삭제) — 전에는 Ctrl-C 가
  `close()` 에 닿지 않아 npx 자식이 전부 고아가 됐다.
- 모델이 막혀도 데몬은 산다: `LLMError` 는 `error` 사건 · `error` 줄 · `turn.end {kind: "error"}`.
  소켓은 0600 으로 태어나고, 살아 있는 소켓이면 거부한다.
- `jobs.Manager` 는 `__init__` 에서 만들어지고(`self.jobs_mgr`, `jobs.bind` 로 묶임) `self._job_record`
  (적은 뒤 그 작업의 세션에 `job.start`/`job.gate`/`job.end` 를 보냄), `push_signal`, step factory 를
  받는다 — 이미 로드된 `self.rules` 를 그대로 넘기지 두 번 로드하지 않는다. 작업은 제 스레드에서
  돌므로 `Manager.dispatch` 가 `record`/`push` 호출을 이벤트 루프 스레드로 옮긴다
  (`call_soon_threadsafe`, `start()` 가 돌 때 한 번 걸림) — 워커 스레드가 `state.apply` 나 소켓을
  직접 건드리지 않게. `close()` 는 먼저 이것을 아무 일도 안 하는 것으로 바꿔서, 종료 중에 끝나는
  작업이 닫히는 `state.db` 와 경합하지 않게 한다.

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
Daemon.run_signal_turn ── 신호마다 rules.decide 를 먼저(절대 안 들려 있고 안 잠든다):
        defer / silence ──▶ 적히고 끝, 공짜
        ask_model ──▶ judgment_cap? defer : rules.judge (모델 호출 하나) ──▶ "judgment" 적고 다시 결정
        speak 인데 today.spoken + 이 다발에서 지금까지 >= daily_budget ──▶ 대신 defer(trust "user" 는 예외)
        speak(남은 것) ──▶ signals.hold_for: 0 ──▶ Daemon._signal_model_turn
                                          >0 ──▶ loop.call_later(wait, "signal_ready" 로 다시 큐에)
                                                  (이게 돌아오는 순간 lane 은 자유다 — 들린 신호 바로
                                                  뒤의 `say` 는 절대 안 기다린다)
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
  천장을 안 두고, 다른 곳은 전부 0). `now_line()`, `day_start(ts)`, `hour(ts)`,
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
  등록한 것을 더한 것(`all_day`, `sender_in`/`sender_domain_in`/`subject_has`, `away`); 로드된
  감각이 없는 종류의 절은 파싱은 되지만 검사는 안 한다. `decide(signal, rules, user_lists,
  today, now)`: 조용한 시간 -> defer, 하루 예산 소진 -> defer(`trust: user` 신호 - 일정이나
  claude_code 종류 자신의 인스턴스 - 는 **둘 다** 예외다: 사용자가 새벽 3시로 정한 알람은
  새벽 3시에도 사용자의 것이다); 그다음 표; 매치 없음 -> `ask_model`. `judge(signal, cfg,
  today)`(D13)는 `worker_model`(있으면) 아니면 `model` 로 요청 하나, 툴 없음, 이력 없음,
  messages = 짧은 고정 시스템 문구("JSON 으로만 답하라: `{\"speak\": ..., \"reason\": ...}`")
  + 그 신호 하나의 봉투; 엄격한 `json.loads`, 그 밖의 무엇이든(막혔거나 미설정인 모델 포함)
  `(False, why)` — 이 모듈이 llm 모듈에 손대는 유일한 자리이고, 이 함수 안에서만이다
  (`test_rules_imports_no_llm_at_top_level`).
- **`deliver.py`** — `Notice(id, session, text, why, signal_keys)`; `Outbox.hold(session)` 은
  `is_running(session)` 이거나, 신선한(60초 이내) `presence` 줄이 `idle_s < idle_threshold_s`
  라고 말하는 동안 참이다. `release()` 는 풀 수 있는 알림을 FIFO 순서로 다 보내고, 아직 걸린
  첫 알림에서 멈춘다. `_deliver` 는 `delivery {id, via}` 와 `assistant {origin: signal, final:
  true}` 를 적고, 붙은 클라이언트가 있으면 소켓 `notice` 를, 없으면 `banner` 를 부른다(기본은
  macOS `osascript` 알림 — 테스트를 위해 갈아 끼울 수 있다). `rebuild()` 는 `state.undelivered()`
  에서 큐를 복구한다; 여기서 곧바로 흘려보내지는 않고 다음 자연스러운 `release()` 계기를
  기다린다 — 재시작이 아무 일도 일어나기 전에 느닷없이 배너를 띄우는 일이 없도록.
- **`senses/`** — 파일마다 150줄 천장(밑줄로 시작하는 파일은 여럿이 나눠 쓰는 어댑터라 이
  배분 밖이다). `schedule.py`: `cfg["schedules"]` 를 읽고(폴링, 30초), 때가 된 줄을 한 번만
  울리고, 반복이면 `config.set` 으로 `at` 을 미루고, 1회성이면 줄을 지운다;
  `add_schedule`/`list_schedules`/`remove_schedule` 을 내놓는다(첫째와 셋째는 `write=True,
  reversible=True`). `shell.py`: push 전용(`build(fields, now)`), `command_failed`(exit != 0) /
  `command_finished`(exit == 0), `exit_nonzero` 와 `duration_over` 를 등록한다; 중복 제거 열쇠에
  도착 시각이 들어가는데, 반복 자체가 `count_over` 가 보려는 것이기 때문이다.
  `calendar.py`/`reminders.py`(캘린더 `poll` 은 아직 시작하지 않은 일정만 신호로 낸다 - 종일 일정이
  폴링마다 다시 울려 하루 92번): 어댑터 하나(`_eventkit.py`, 200줄, PyObjC 의
  `EventKit`/`Foundation` 을 함수 안에서 그때그때 임포트 - Mark.5 의 `eventkit_bridge.py` 를
  목록/추가만 남기고 잘랐고, 미리 알림에는 완료/삭제를 더했다(D38: 둘 다 `write=True` 라 묻고
  나서 하며, 미리 알림이 아닌 id 는 `LookupError`), 시각은 평범한 epoch 부동소수라 `clock.py` 와 맞출 두 번째 날짜
  형식이 없다; 도구 결과는 `for_model()` 을 거쳐 로컬 `YYYY-MM-DD HH:MM` 으로 나간다 - 모델이
  epoch 를 잘못 읽었기 때문이다)를 감각 둘이 나눠 쓴다; 나눈 이유는 `TRUST` 가 다르기 때문이다(일정은 남의
  초대일 수 있어 `third_party`, 미리 알림은 사용자 자신의 메모라 `user`) - 감각 모듈 하나에
  `TRUST` 문자열 하나뿐이라 한 파일에 같이 못 둔다. PyObjC 없이는 꺼짐: `poll` 이 한 번
  예외를 내면(`SenseLoop.on_error` 가 `error` 사건으로 적는다) 그다음부터는 조용히 `[]` 를
  돌려주고, 툴들도 같은 문구를 내는데 브로커가 다른 핸들러 예외와 똑같이 받는다. `mail.py`:
  IMAP 헤더만(`imaplib`, `BODY.PEEK[HEADER...]`, 읽기 전용 `SELECT` - `poll` 은 본문을 절대
  안 가져온다), 계정 비밀번호는 macOS 키체인에서(`security find-generic-password`) - config
  나 환경변수가 아니다; 메모리 안의 UID 워터마크가 이번 프로세스에서 이미 신호로 만든 메일의
  재조회를 막고, 진짜 24시간 중복 제거는 `state.seen` 이 한다. `attach.py`: 데몬 자신의
  `attach {cwd}` 메시지가 먹이고, 훅이 아니다 - 항상 침묵, rules 와 훗날의 카탈로그가 사용자가
  뭘 하고 있는지 알기 위해서만 존재한다. `claude_code.py`: Claude Code 의 `Stop`/`Notification`
  훅이 push 하고, `away` 를 등록한다(`state.Today.away` 를 읽는데, 데몬이 `decide` 를 돌리기
  전에 `presence` 로부터 계산해 둔 것이다 - 조건 술어는 `(signal, value, today, now)` 만 보지
  `rules` 나 `state` 자체는 못 보므로, `idle_threshold_s` 비교는 술어가 아니라 `state.today()`
  안에서 해야 한다). `docs/senses.md` 에 zsh 훅(백그라운드로 돌리고 오류를 삼킴 — 훅이 셸을
  막으면 안 된다)과 Claude Code `settings.json` 훅 블록이 있다.
- **`config.py`** 이 더한 것: `senses`(`{name: options}` — 키가 있으면 켜짐; `_check` 가 그
  감각 모듈을 임포트해 옵션을 `OPTIONS` 로 검사)와 `schedules`(`{id, at, repeat, text}` 목록;
  `at` 은 이미 `clock.parse_local` 의 표준 문자열이어야 한다).
- **두 번째·세 번째 외부 의존.** `calendar`/`reminders` 는 `pyobjc-framework-EventKit` 이
  필요하다; `mail` 은 추가 패키지는 없지만(`imaplib` 은 표준 라이브러리) 키체인 항목
  (`docs/senses.md`)과 IMAP 서버로의 네트워크 접근이 필요하다. 둘 다 선택 사항 - 나머지는
  이것들 없이도 돈다.

## 9. 작업자 (`pathfinder/jobs.py`, `pathfinder/llm/slot.py`)

- **`start_job(goal)`** 은 `origins={"user"}` 인 `ToolSpec`(§5)이고, `jobs.py` 가 임포트되는
  순간 `tools.TOOLS` 에 등록된다. 핸들러는 (데몬이 심어 둔) `control.session` 을 읽어
  `jobs.Manager.start(goal, session)` 을 부르며, `jobs.bind(manager)` 로 한 번 묶인다 —
  `signals.load` 가 감각 모듈에 `HOME` 을 심는 것과 같은 방식이다.
- **`Manager(home, record, push, step_factory, rules)`** 가 작업 전부를 쥔다: `dict[id, Job]`
  (`Job`: `id, goal, session, control, messages, started_ts, state, steps, usage`; `state` 는
  `running|gated|done|failed|cancelled`), 자기만의 읽기 전용 `State(home/"state.db")`
  (`today().jobs` 와 재시작 훑기용 — 실제 쓰기는 여전히 데몬의 State 가 한다), 그리고 데몬이
  이미 로드해 둔 `Rules`(`jobs_per_day`, `job_steps` — RULES.md 앞머리).
- **`start(goal, session)`** 은 작업이 이미 `running`/`gated` 이거나(한 번에 하나 — 무료
  플랜은 슬롯이 하나고 D25 가 이미 호출을 직렬화한다) `today.jobs >= jobs_per_day` 면 오류가
  아니라 평범한 문자열로 거절한다; 아니면 `job_start {id, goal}` 을 적고, 메시지 =
  `prompts/job.txt` + `clock.now_line()` + `Goal: ...` 를 짓고, 새 스레드에서 `_drive` 를 돈다.
- **`_drive`** 는 `Broker(tools.for_origin("job"), "job", ...)` 와 `step_factory(cfg, control,
  job_tools)` 에서 나온 step(제품판: `default_job_step_factory` — `worker_model` 이나 `model`,
  `llm.slot.stream_step(..., "job")`)을 짓고 `loop.run(..., budget=rules.job_steps)` 을 돈다.
  결말 `answer`/`exhausted` → `job_done {id, summary(2000자로 자름), steps, usage, exhausted?}`;
  `cancelled` → `job_failed {reason: "stopped"}`; `LLMError` → `job_failed {reason: "model:
  ..."}`; 그 밖의 예외 → `job_failed {reason: "internal: <타입>"}`. 두 결말 다
  `Signal(sense="jobs", kind="job_done"|"job_failed", trust="self", exempt=True, ...)` 을 push
  한다(D26: 요약은 모델의 글이라 봉투 안 권위는 없고, 사용자가 시킨 작업이라는 면제 — 예산·조용한
  시간 — 는 `exempt` 가 든다) — `job.state` 는 장부에 적고 push 한 *뒤에* 바뀌므로, 지켜보는
  쪽은 그 사실이 디스크에 있기 전에 상태가 바뀐 걸 절대 못 본다.
- **게이트(D24)**: `loop.run` 에서 올라온 `Gate` 는 `approval_pending {..., job: id}` 을 적고
  그 작업을 `gated` 로 만든다; 스레드는 끝나고 lane 은 비어 있다. `job` 이 있는 줄에 대한
  `approve`/`deny` 는 세션 lane 을 아예 건너뛰고 `Manager.resume(approval_id, decision)` 을
  부른다 — 저장된 줄에서 지은 `approved`/`denied` 로 새 스레드에서 `loop.run(resume=True)` 를
  다시 돈다 — 재개된 작업도 다시 게이트에 걸릴 수 있다.
- **재시작 훑기**(`Manager.sweep()`, 루프가 돈 뒤 `Daemon.start()` 가 부른다 — `__init__` 에서 밀면
  받을 lane 이 없다): 끝 사건이 없는 `job_start` 는 (그 id 가 대기 중인 승인
  줄에 있으면) `gated` 로 되살리거나(메시지는 그 줄에서 돌아온다) `job_failed {reason: "daemon
  restarted"}` 로 닫는다 — 크래시가 대기 중인 게이트를 조용히 잃는 일은 없다.
- **`stop(id)`** 는 `control.interrupt` 를 세운다(도는 셸을 interrupt 된 턴과 똑같이 죽인다);
  게이트에서 기다리는 작업은 지켜볼 스레드가 없으므로 `stop` 이 직접 대기 승인을 거절로 닫고 작업을
  `cancelled` 로 끝낸다. **`steer(id, text)`** 는 `user {steer: true, job: id, origin: "user"}` 를 적고(작업이 아니라
  사용자가 말한 것 — `state` 는 이것을 세션 대화 이력에 넣지 않는다)
  사용자가 한 말이므로) `daemon.steer` 와 똑같이 큐에 넣는다.
- **`llm/slot.py`**: `slot(priority)` 는 스트리밍 요청 하나를 감싸는 컨텍스트 매니저다. `user`
  호출자는 지금 쥐고 있는 쪽만 기다리고, `job` 호출자는 사용자가 기다리거나 쥐고 있는 동안도
  같이 기다린다 — 그래서 사용자의 턴이 먼저 물은 작업 뒤에 줄 서는 일이 없다(m7a, HISTORY:
  무료 플랜은 겹치는 두 번째 요청을 서버에서 줄 세운다 — 슬롯은 오류 보험이 아니라 순서
  장치다). `stream_step(endpoint, schemas, control, priority)` 가 `openai_compat.stream` 을
  거기 감싼다; `daemon.default_step_factory` 는 유저 턴과 신호 턴 둘 다 우선순위 `"user"` 를,
  `jobs.default_job_step_factory` 는 `"job"` 을 쓴다.
- **CLI**: `pathfinder jobs`(id, state, started, goal — `state.jobs()` 에서), `pathfinder job
  stop ID`, `pathfinder job steer ID TEXT`; `pathfinder approvals` 는 있으면 `job: <id>` 를
  찍는다; `pathfinder listen` 은 `job.start`/`job.gate`/`job.end` 줄도 찍는다.
- **`state.py` 가 더한 것**: 표 `jobs(id, session, goal, started_ts, ended_ts, state, summary,
  reason, steps, usage_json)`, 세 작업 사건에서만 투영된다(`gated` 는 여기 안 적힌다 — 그건
  `Manager` 자신의 메모리 상태이고, 대기 중인 승인의 `job` 칸으로 대신 찾는다); `approvals` 에
  `job` 칸이 늘고; `jobs()`, `job(id)`, `running_jobs()`. `Today` 에 `jobs`(오늘의 `job_start`
  수, `clock.day_start` 기준)가 늘었다.
- **`senses/jobs.py`** 는 `TRUST = "self"` 와 `KINDS = {job_done, job_failed}`(둘 다 기본
  RULES.md 에서 `always -> speak` — D8 §8: 사용자가 시작한 것의 결말은 예산도 안 쓰고 조용한
  시간도 안 따진다, `Signal.exempt` 가 든다)를 선언하는데, `Manager` 자신의 `Signal` 객체가 이미 맞추고 있는 모양을
  문서로 남기기 위해서일 뿐이다; `build` 를 안 내보내므로 `push_capable` 이 절대 안 되고,
  소켓에서 온 `signal jobs ...` 는 모르는 감각과 똑같이 거부된다.

## 10. 기억 (`pathfinder/memory.py`)

볼트는 `<홈>` 아래 세 종류 파일이다 - `USER.md`, `RULES.md`(구획 3부터 이미 있었음)와 이제
`wiki/*.md` - `vault/` 디렉터리는 없다: "볼트 안"은 이 셋에 대한 경로 검사(`vault_path`)이고
`..`, 절대경로, 그 밖의 무엇이든 거부한다.

- **툴**, `jobs.py` 처럼 임포트 시점에 등록됨: `search_memory(query)`(하이브리드
  `state.search`, `embed_model` 이 비어 있거나 임베딩이 실패하면 FTS 만), `read_memory(path)`,
  `read_ledger(after, limit=80)`(패스가 페이지로 넘겨 읽는 번호 매긴 조각, 읽을 수 있는 사건마다
  `render_line` 한 줄, 툴 잡담은 건너뜀), `edit_memory(target, op, old, new, evidence)`
  (`reversible=True`, D22). `search_memory`/`read_memory` 는 다섯 origin 모두에 열려 있고;
  `read_ledger` 는 `memory` 전용; `edit_memory` 는 `{user, memory}` - 사용자 턴 자신의 호출은
  그 자체로 돈다(정책: user/reversible = allow), 패스도 마찬가지, 그 밖엔 아무도 못 본다.
- **게이트(`Manager.vet`, 모델 없음)**: 대상이 볼트 안; `op` 는 `add|replace|remove` 중 하나이고
  `new`/`old` 는 필요에 따라 비어 있으면 안 됨; `old` 는 지금 파일의 정확히 한 줄과 맞아야 함;
  수정 후 파일이 여전히 파싱됨(`RULES.md` 는 로드된 감각들의 어휘로 `rules.parse` 를 지남;
  `USER.md` 의 앞머리는 세 목록을 유지해야 함); `evidence` 는 부르는 쪽이 실제로 읽은 seq
  들이다(패스라면 `read_ledger` 가 쌓은 자기 `allowed` 집합, 사용자 턴이라면 - 인용할 것이 없어
  - 그 세션의 최신 `user` 메시지가 자동으로 채워짐); 패스가 하는 `USER.md` 수정은 origin 이
  `user` 인 근거가 있어야 함(`test_user_facts_need_user_origin`). 검사에 실패하면 그 이유가
  툴 결과로 돌아간다; 아무것도 안 쓴다.
- **경로와 적용**: `wiki/*.md` 수정은 그 자리에서 적용된다(원자적 쓰기, 그 뒤
  `state.index_page` 가 다시 임베딩); `USER.md`/`RULES.md` 는 승인 상자로 간다
  (`approval_pending {tool: "edit_memory", args: proposal, messages: []}`, 툴 게이트와 같은
  표) - 호출이 **사용자 턴 자신의 것**이고 `grants["edit_memory"]` 가 `"allow"` 일 때만 예외다
  (D28; 패스 자신의 제안은 늘 상자로 간다). `Manager.apply` 가 파일을 쓰고 `memory_apply
  {target, op, old, new, evidence, approval?}`(되돌리기 기록 전부)를 적으며, `RULES.md` 라면
  `on_rules_changed()`(아래 `reload_rules`)를 부른다. `Manager.decide(approval_id, decision)`
  이 데몬의 세 번째 `approve`/`deny` 경로다: 승인되면 지금 파일 상태에 맞춰 다시 적용하고,
  거부되면 아무것도 안 쓴다. `undo(n)`(CLI 전용)은 최근 `memory_apply` n개를 거꾸로 역연산으로
  되돌린다(`add` -> `new` 를 remove, `replace` -> `new` 를 `old` 로 replace, `remove` ->
  `old` 를 add), 각각 새 `memory_apply` 로 다시 적힌다 - 별도 볼트 이력은 없다, 장부가 이미
  그 역을 갖고 있다.
- **상주 문맥**: `context(home, state)` = `USER.md` 의 본문 + wiki 지도(`- 제목 (날짜)`, 최신
  먼저, 1,000자에서 자름); 데몬이 이것을 매 턴의 *두 번째* 시스템 메시지에 `clock.now_line()`
  뒤에 붙인다 - 상주하는 첫 메시지는 바이트 단위로 그대로고, 두 번째만 이미 턴마다 바뀐다.
- **패스(`Manager.run_pass`)**: 작업자처럼 제 스레드에서, 사용자 자신의 턴과 별도로 도는
  `loop.run` 턴 하나(우선순위 `"job"`). messages = `prompts/consolidate.txt`(바이트 고정) +
  `now_line()` + 장부 범위와 닫힌 조건/행동/종류 어휘, 그리고 살아 있는 세션의 이전 요약(있으면)을
  담은 목표 메시지. 툴 = `tools.for_origin("memory")`(정확히 `search_memory, read_memory,
  read_ledger, edit_memory`); 예산 `consolidate_steps`. 마지막 답이 `session_summary {session,
  upto, text}` 가 된다, `upto` = 그 세션의 다섯 번째로 최신인 메시지의 seq(최근 넷은 그대로
  남는다). 그다음 `memory_pass {since, upto, read, steps, usage, applied, proposed}` -
  내부 오류는 커서를 전진시킨다 - 스텝을 깨는 조각이 영영 되풀이되지 않도록; **한 일 없이
  장애로 끝난 패스는 커서를 그대로 둔다**(`LLMError` status 0 · 401 · 402 · 408 · 429 · 5xx;
  `upto = since`, D39 - 오프라인에 대표 자신의 말이 빠졌다) 그리고 방아쇠는 30분 기다렸다 다시
  해 본다, 그 밖의 4xx 는 전진한다; `proposed > 0` 이면 `memory_proposed` 를 push. 방아쇠(`Manager.tick`, 60초 스레드,
  `Daemon.start`/`close`)는 도는 턴이 없고 작업도 슬롯을 안 쥐고(`is_busy`), 최신 user 메시지가
  `consolidate_idle_min` 분보다 오래됐고, 안 읽은 줄이 `consolidate_min_events` 이상일 때 패스를
  시작한다; `pathfinder memory run` 이 소켓으로(`memory.run`) 하나를 강제하고 `run_pass` 자신의
  결과 줄을 돌려준다.
- **`memory_proposed`/`memory_stalled`** 는 `Manager` 가 직접 push 한다(`senses/memory.py` 는
  모양만 문서로 남기고 `build` 는 안 내보내, `signal memory ...` 는 `jobs` 처럼 거부된다).
  `tick` 은 사흘 연속 로컬 하루가 각각 뭔가 읽고 아무것도 적용하지 않았을 때도
  `memory_stalled` 를 올린다(청사진 §9: Mark.5 의 24일 침묵이 3일이 된다), 하루에 한 번으로
  줄인다(`_stalled_key`) - 틱마다 그냥 push 하면 막힌 동안 60초마다 `signal` 줄이 쌓인다.
- **`reload_rules`**(`Daemon`)는 `RULES.md` 를 다시 파싱해 `self.rules` 를 outbox, 작업 매니저,
  기억 매니저에 다시 묶는다; `ValueError` 면 옛 규칙을 지키고 `error` 사건을 적는다. 승인된
  `RULES.md` 수정 뒤, 그리고 신호 턴 심사마다 한 번(`stat` 하나) 손 수정이 mtime 을 옮겼을 때
  불린다 - HISTORY 의 "데몬 시작 때 한 번만 읽음"을 닫는다.
- **CLI**: `pathfinder memory`(커서; 오늘과 그 전 이틀의 passes/read/applied; 대기 중 제안;
  이레치 판정 수; 최신 세션 요약의 `upto`); `pathfinder memory run`; `pathfinder memory undo
  [N]`(데몬이 도는 중엔 `reindex` 처럼 거부; 독립된 `Ledger`/`State`/`Manager`). `pathfinder
  approvals` 는 `edit_memory` 줄을 `target op: old -> new` 로 찍는다(양쪽 다 60자에서 자름);
  `reindex` 도 `memory.reindex_pages` 를 불러 페이지 수를 알린다.

## 11. 도구 경로 (구획 6): MCP 클라이언트 · 카탈로그 · 설치기

남이 만든 도구를 승인 한 번에 산다. 경로는 레지스트리 → 후보 → 태그 → 라이선스 → `tool_candidate`
알림 → "설치해" → 검사(런타임, 고정 버전+무결성, 라이선스, 비밀, 툴을 나열·해시하는 샌드박스
시험 기동) → `install_server` 제안 행 하나 → `approve` → 설정 + Claude Code + 핀 + grants + 적재 →
`install_done` 알림 → 다음 턴이 `mcp__<server>__<tool>` 을 부른다. 모든 걸음이 사건이고,
`state.db` 를 지우고 `reindex` 하면 핀·설치·대기 중인 행이 돌아온다.

- **`pathfinder/mcp/`(Mark.5 `brain/mcp/` 이식; 코어가 아닌 어댑터 예산)**: `protocol.py`(두
  규격 세대의 JSON-RPC 봉투, `mcp__<server>__<tool>` 이름 규칙 - 툴 이름에 점이 올 수 있고 서버
  id 에 밑줄이 못 오므로 `__`, `sanitize`), `transport.py`(stdio: argv, 호출자가 준 env, stdout
  펌프 스레드, stderr 배수, id 로 짝지은 응답; Streamable HTTP: 요청마다 POST 하나, JSON 또는 SSE
  응답), `session.py`(`discover`/`list_tools`/`call_tool`; **두 세대 probe**: 설정의 `protocol`
  키가 있으면 그것이 이기고, 없으면 전송으로 추측 - stdio → 옛 `initialize` 핸드셰이크, HTTP →
  무상태 `server/discover` - JSON-RPC 오류면 한 번 뒤집는다; 타임아웃은 재시도하지 않는다 -
  Mark.5 실측에서 옛 서버는 `server/discover` 에 아예 답하지 않았다; probe 는 `_call` 의 첫
  접촉에서 돌아 `entries()` 가 건너뛸 수 없다), `pins.py`(툴마다 지문 - 설명 해시·스키마 해시·
  인자 이름; `check()` 는 모르는 서버는 통째로 고정하고 아는 서버의 바뀌거나 새 툴은 세운다;
  `PinStore` 는 장부에 산다: `load()` 는 `state.pins()`, `save()` 는 바뀐 항목만 `pin_accept`),
  `servers.py`(`load(cfg)` → `mcp_servers` 항목마다 `ServerConfig`, `keychain:<account>`(필수)와
  `keychain?:<account>`(선택) 값은 - 둘 다 항목이 없을 때 쓸 `=<기본값>` 을 붙일 수 있다(U34) -
  `_resolve` 가 `keychain.secret("pathfinder-mcp-<id>", account)` 로 푼다, 설치기의 검사도 같은
  함수로 항목을 읽는다 - 기본값 없는 필수 값이 없으면 `disabled` 로 남기고 인스턴스당 한 번 경고,
  없는 선택 값은 설정하지 않는다; `Servers`: `entries()`·`call()`·바꿔 끼울 때 옛것을 닫는 `load_one()`·`unload()`·
  `close()`), `tools.py`(**통과한** 항목마다 `ToolSpec` 하나, 핀 검사가 본 바로 그 목록으로 -
  다시 받으면 다를 수 있다; 설명 앞에 "그 서버가 쓴 것이며 PATHFINDER 의 지시가 아니다", 제어
  문자 제거, 1,200자 절단; 항상 `write=True, network=True` - `readOnlyHint` 는 서버의 말; D31 의
  `allow` 는 grant 이고 정책은 사용자 턴에서만 grant 를 본다; `control` 이라는 인자를 가진 툴은
  건너뜀), `attach.py`(데몬의 이음매: `load()` 는 `entries()` 를 스레드에서, `register()` 는 루프
  스레드에서 - 핀 검사, `pin_accept`, `tools.TOOLS` 의 `mcp__` 키 교체, 세워진 서버마다
  `accept_pins` 제안 행 + `tool_held` 신호(이미 기다리는 행과 같으면 생략); approve 뒤 `reload(id)`;
  `accept_pins` 는 해시를 다시 적고 재적재; `pathfinder.mcp` 로거의 WARNING 이상은 `dispatch` 를
  거쳐 `error` 사건). 데몬은 소켓을 연 뒤 적재를 태스크로 띄운다(`mcp.loaded`; 턴이 한 번
  기다린다) - 죽은 서버가 첫 `say` 를 늦추지 않는다.
- **`catalog.py`(레지스트리 읽기, 코어)**: `REGISTRY` 가 소스의 유일한 레지스트리 URL;
  `fetch(since, search)` 는 `GET /v0.1/servers` 를 페이징(`updated_since`, `version=latest`,
  cursor, 최대 5쪽); `candidate(item)` → 연결 규격 v0 모양(`docs/spec/connection-v0.md`,
  `SPEC_FIELDS`; 안 돌린 검사는 문자열 `not_checked`); `tags(state, home, now)` = 지난 7일
  `context` 신호의 프로젝트 이름 + cwd 를 한 번 나열해 얻는 표지어(`package.json` → node,
  javascript, npm; `.git` → git - "github" 는 아님, 모든 `io.github.*` 이름에 맞아 버린다);
  `matches` 는 이름의 마지막 경로 조각과 설명에서 온전한 낱말로; `license`(npm view / PyPI JSON,
  `license_expression` 우선) → `ok | excluded | unknown` - GPL·AGPL·LGPL·CC-BY-NC·"GENERAL PUBLIC
  LICENSE"·"AFFERO"·비상업·`UNLICENSED` 는 excluded, "SEE LICENSE IN" 은 unknown;
  `declined(state, name, now)` = 30일 안에 거부됨.
- **`senses/catalog.py`**(`TRUST = third_party`, `CADENCE_S` 24시간): `poll` 은 마지막
  `tool_candidate` 신호 이후(최대 일주일)를 받아, 태그에 맞고 라이선스가 `ok`(폴당 조회 10회
  까지)이며 설치·거부되지 않은 것만 → `tool_candidate {candidate, matched_tags}`, 키
  `catalog:<name>@<version>`. 기본 규칙: `count_over 3 → silence`(홍수는 레지스트리 덤프),
  `always → speak`. 이 감각의 `TOOLS`(감각이 켜진 동안만 등록): `search_tools(query)`(사용자
  출처, 그 자리에서 레지스트리; 레지스트리는 하이픈 이름의 부분 문자열로 찾으니 공백은 `-` 로,
  결과 열 개와 60초 타임아웃 - 드문 이름은 표 전체를 훑는다 - 이고, 없으면 `[]` 대신 문장)와 `propose_install(name)`(사용자 출처만 - 신호·작업 턴은 보지도
  못한다; 검사는 턴의 스레드에서 돌고 제안이 상자에 앉는다). `tool_held`·`install_done`·
  `install_failed` 는 `attach`/설치기가 직접 민다(`trust self`; 설치 결말은 작업처럼 `exempt` -
  사용자가 시킨 일).
- **`installer.py`(코어, 약 480줄)**: `check(candidate, explicit=)` 는 `verification` 을 순서대로
  채우고 첫 결정적 실패에서 멈춘다 - 런타임(`shutil.which`, 이유에 고치는 한 줄), 고정 버전+
  무결성(`npm view ... dist.integrity license --json` 한 번 / PyPI digests; `latest` 는 절대 아님),
  라이선스(같은 조회의 값으로; `excluded` 는 카탈로그 제안이면 실패, 명시 요청이면 행에 보이는
  경고, D32), 비밀(`secret` env 는 전부 Keychain 에 이미 있어야 하고, 없으면 행에 `needs: NAME`
  과 `security add-generic-password` 줄; 시험은 없이 돈다), **시험 기동**(stdio 명령을 `exec.py`
  의 Seatbelt 프로파일로, 쓰기는 새 임시 디렉터리로 한정 - `npm_config_cache`/`UV_CACHE_DIR` 을
  그 안에 두어 새 HOME 에서도 설치가 됨 - `discover`+`tools/list` 60초 타임아웃, 툴마다 해시와
  `readOnlyHint`, 프로세스 종료, 디렉터리 삭제), 그리고 `install` 블록(`permissions`: readOnly
  힌트 툴은 `allow`, 나머지 `ask`). `propose` 는 `approval_pending {tool: "install_server", args:
  candidate, messages: []}` 하나를 적는다(D3); `pathfinder approvals` 는 `license MIT ok ·
  integrity ok · trial ok, 6 tools (3 read-only) · needs: TOKEN` 으로 그린다. `decide` 가
  `PROPOSALS["install_server"]` 핸들러: 승인 → 스레드에서 `install`, 단계마다 `install_step {id,
  step, ok, detail}` - `config.set` 으로 `mcp_servers`, Claude Code 병합(백업, `mcpServers[<id>] =
  {command, args, env: {NAME: "${NAME}"}}`, 원자적 쓰기, 되읽어 다른 키 전부 deep-compare), 시험
  툴마다 `pin_accept`, readOnly 툴의 `grants`(D31), 루프에서 `attach.reload(id)` - 그리고
  `install_done` + 신호; 어느 단계든 실패하면 역순 `rollback`(unload, `pin_revoke`, 백업에서
  `~/.claude.json` 바이트 복원, `mcp_servers`·`grants` 복원) 뒤 `install_failed {id, step, reason}`
  + 신호. `uninstall(id)` 는 완료된 설치의 역경로(`uninstalled`) - 단 `~/.claude.json` 은 제자리에서
  고친다(`mcpServers[<id>]` 를 빼고 원자적으로 쓰고 되읽어 비교): 설치 때의 백업은 몇 초 뒤의
  `rollback` 용이고, 나중에 그것을 되살리면 다른 서버 항목과 Claude Code 자신의 기록이 지워진다
  (리뷰). 서버 id 는 검사 때 한 번 정한다(레지스트리 이름의 마지막 조각을 설정의 id 규칙으로);
  이미 쓰인 id 면 설치가 실패한다. 검사는 `npx`/`uvx` 외의 런타임, 고정된 `<identifier>@<version>`
  을 정확히 담지 않은 argv(레지스트리의 `runtimeArguments` 는 남의 텍스트), 정확하지 않은 버전을
  거부한다. 거부된 행이
  `catalog.declined` 가 읽는 것이다.
- **제안 상자**(`daemon.py`): `messages` 가 빈 행은 제안; `PROPOSALS = {"edit_memory",
  "accept_pins", "install_server"}` 가 툴 이름으로 핸들러를 고른다(동기든 await 든); messages
  가 있는 행은 lane 게이트라 턴을 잇고, `job` 이 있는 행은 작업을 잇는다. 사용자 자신의
  턴이 낸 제안(origin `user`)은 lane 게이트와 같은 `gate` 줄을 그 세션에 보낸다(`install_server`
  는 `installer.brief` 한 줄로, 레지스트리 글이 터미널에 닿으니 제어 문자는 뺀다) - 문은
  `Daemon.record` 하나; 패스나 attach 의 제안은 제 알림
  뒤에 그대로 둔다.
- **`state.py`**: `pin_accept`/`pin_revoke` 로 `pins(server, tool, hashes, params)`; 설치 사건
  넷으로 `installs(id, name, version, state, ts, detail)`(`detail` 은 백업 경로);
  `installed_names()`, `install(id)`, `context_cwds(since)`.
- **CLI**: `pathfinder install NAME`(소켓; 검사는 데몬 스레드; 행을 출력), `uninstall ID`,
  `search TEXT`(데몬 불필요), `tools`(설정+핀에서 서버·툴·grants, 세워진 툴 표시), `approvals`
  (제안 종류마다 제 줄).
- **v1 에 없는 것**(D14 와 계약): 팀원 서버로 결과 전송(규격에 `outcome` 이 있고 보내지는
  않는다), Cursor/Claude Desktop, OAuth, mcpb 번들, 자동 갱신(새 버전은 새 후보), 악성 코드
  검사(`malicious_scan: not_checked`), 상태 있는 옛 HTTP 서버의 `Mcp-Session-Id`.

## 12. 불변조건과 그것을 붙드는 테스트

| 불변조건 | 테스트 |
|---|---|
| 장부에 쓰는 길 하나 · 파티션 함수 하나 · 설정에 쓰는 길 하나 · 날짜 계산은 다른 데 없음 | `test_invariants.test_single_writer` |
| 핀은 장부에 산다(`state.db` 를 지워도 핀이 남고, `pin_accept` 는 바뀐 핀만); 바뀐 툴은 `approve` 전까지 세워지고 등록되지 않는다; probe 는 실제 경로에서 돈다 | `test_mcp_pins.test_pins_live_in_the_ledger`, `test_daemon.McpDaemonTest.test_rug_pull_holds`, `test_mcp_client.test_list_tools_runs_the_probe_first` |
| 비밀은 `config.json` 에 닿지 않는다; 없으면 서버가 크게 꺼진다 | `test_mcp_servers.test_secret_from_keychain_never_in_config` |
| 설치는 `~/.claude.json` 을 비파괴로 병합하고 바이트 그대로 되돌린다; grants 는 승인 때 readOnly 힌트 툴에만; 카탈로그는 제외 라이선스를 제안하지 않는다; `propose_install` 은 사용자 전용 | `test_installer.*`, `test_daemon.InstallerDaemonTest` |
| 소스의 레지스트리 URL 은 하나; `catalog.py` ≤ 300 · `installer.py` ≤ 700 · `senses/catalog.py` ≤ 150 · `mcp/attach.py` ≤ 120 · `daemon.py` ≤ 780; 규격 fixture 는 v0 필드를 전부 가진다 | `test_invariants.*`, `test_spec_fixtures.*` |
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
| `judge` 는 엄격한 JSON 만 받고, 쓰레기/필드 누락/막히거나 미설정인 모델에는 `(False, ...)`; `worker_model` 을 `model` 보다 먼저 읽음 | `test_rules.JudgeTest.*`, `test_daemon.JudgmentCapTest.test_judge_never_runs_over_cap` |
| calendar/reminders 는 PyObjC 없이 죽지 않고 꺼지고, 실패를 한 번만 적고, 그다음은 조용함; 툴도 같은 문구를 돌려줌 | `test_calendar.CalendarOffTest.*` |
| mail 의 `poll` 은 본문 fetch 를 절대 안 함(`PEEK[HEADER` 뿐); 비밀번호는 키체인에서만 오지 config 키가 아님 | `test_mail.MailHeadersOnlyTest.*` |
| `attach {cwd}` 는 감각이 켜져 있을 때만 `context` 신호를 정확히 하나 push 함 | `test_daemon.AttachSenseTest.*` |
| `claude_code` 의 `task_done`/`needs_input` 은 `today.away` 가 참일 때만 말함 | `test_claude_code.ClaudeCodeAwayTest.test_claude_code_speaks_only_when_away` |
| `local_datetime` 게이트는 모델이 쓴 것이 아니라 표준 문자열을 보여줌 | `test_calendar.AddEventGateTest.test_add_event_gate_shows_canonical_datetime` |
| 작업이 기다릴 때는 FIFO 가 아니다: 사용자는 먼저 기다리던 작업보다 앞선다; 슬롯은 스텝 사이에 풀린다 | `test_slot.SlotTest.*` |
| `start_job` 은 유저 전용; 작업과 신호 턴 자신의 레지스트리엔 절대 없음 | `test_tools.OriginRegistryTest.test_tools_for_origin` |
| 작업이 요약까지 돌고 그 툴 사건은 `origin: job` 을 닮; 예산 초과와 중복 실행을 거절; `stop` 이 막힌 툴을 1초 안에 죽임; steer 가 다음 스텝에 닿음; 게이트가 서고 `approve`/`deny` 둘 다 올바르게 재개함; 게이트가 재시작을 견디고, 게이트 없이 죽은 것은 `job_failed` 로 닫힘; `LLMError` 는 작업만 실패시킴; 스텝 예산 소진은 `exhausted: true` 를 단 `done` | `test_jobs.*` |
| `state.db` 삭제: 작업과 그 게이트 승인(`job` 칸 포함)이 장부만으로 돌아옴 | `test_jobs.ReindexRestoresJobsTest.test_reindex_restores_jobs_and_job_approvals` |
| 작업이 도는 중에도 세션 자신의 턴은 여전히 답함; `job_done` 은 `exempt` 라 하루 예산이 다 쓰였고 조용한 시간이어도 말함 — trust 는 `self`, 결코 `user` 가 아님 | `test_daemon.JobDaemonIntegrationTest.test_daemon_answers_while_job_runs`, `test_daemon.JobSignalTest.test_job_done_speaks_without_budget` |
| 소켓이 `signal jobs ...` 를 거부함; `jobs.py` 는 350줄 이하 | `test_daemon.PushDoorValidationTest.test_socket_refuses_signal_jobs`, `test_invariants.test_jobs_module_under_350_lines` |
| 볼트는 정확히 `USER.md`, `RULES.md`, `wiki/*.md` 뿐; 게이트는 볼트 밖 · 모르는 RULES.md 단어 · 정확히 안 맞는 `old` 를 거부; 패스의 USER.md 수정은 `origin: user` 근거가 필요 | `test_memory.test_vault_path_refuses_outside`, `test_vet_refuses_unknown_rule_word`, `test_vet_replace_needs_exactly_one_line`, `test_user_facts_need_user_origin` |
| wiki 수정은 그 자리에서 적용됨; USER.md/RULES.md 는 사용자 턴 grant 가 허락하지 않는 한 승인 상자로 감; RULES.md 수정을 승인하면 `reload_rules` 가 불림; undo 는 역연산 | `test_memory.test_wiki_applies_at_once`, `test_user_and_rules_go_to_the_box`, `test_grants_allow_applies_user_turn_at_once`, `test_rules_apply_calls_on_rules_changed`, `test_undo_is_the_inverse_op` |
| 패스는 제 조각만 읽고, 근거는 그 조각에서만 나와야 하며, 내부 오류는 커서를 전진시키고 모델 오류(한 일 없음)는 그대로 둠; 방아쇠는 유휴와 충분한 안 읽은 줄을 기다림; 사흘 침묵이면 `memory_stalled` 를 한 번만 올림 | `test_memory.test_pass_reads_slice_and_advances_cursor`, `test_pass_llm_error_keeps_the_slice`, `test_pass_evidence_must_be_from_its_slice`, `test_due_waits_for_idle_and_enough_lines`, `test_stalled_after_three_silent_days` |
| 옮겨온 말뭉치에서 하이브리드 검색이 D10 기준만큼 FTS 만보다 나음(아니면 벡터는 기본으로 꺼짐); 낡은 모델의 벡터는 절대 안 맞음; 세션 이력은 요약 뒤로 접힘; `state.db` 삭제는 페이지 · 요약 · 기억 커서를 복구함 | `bench/m4_retrieval.py`, `test_state.test_search_hybrid_rrf_order`, `test_search_ignores_vectors_of_another_model`, `test_history_folds_after_summary`, `test_reindex_restores_summaries_and_applies` |
| `edit_memory` 승인은 세션 lane 이 아니라 기억 매니저로 감; RULES.md 는 승인된 수정이나 손 수정 뒤 재시작 없이 다시 읽히고, 파싱 오류면 옛 규칙을 지킴; 상주 문맥은 USER.md 본문과 wiki 지도를 턴의 두 번째 시스템 메시지에만 싣고 첫 메시지는 그대로임 | `test_daemon.MemoryDaemonTest.*` |
| `memory_proposed`/`memory_stalled` 는 기본이 침묵이고 기본 RULES.md 는 둘 다 말함 | `test_senses.MemorySenseTest.test_memory_kinds_default_silence_and_shipped_rules_speak` |
| `init` 은 첫 실패 검사에서 멈추고 Ollama 가 답하기 전엔 아무것도 쓰지 않음; 모델 선택은 `config.set` 을 거침; 감각은 병합, 교체 아님; zshrc 한 줄과 Claude Code 훅은 한 번만 들어감(두 번째 실행은 파생 파일을 바꾸지 않음); plist 는 인터프리터 · 리터럴 `PATH` · 로그를 담음; 테스트의 `service` 는 진짜 launchd 를 절대 안 부름; `init.py` 에 모델 이름 없음 | `test_init.*` |
| TUI 는 같은 프로토콜로 `say`/`approve`/`react`/`interrupt` 를 보내고, `presence` 를 생산하며(타이핑 = 0, 침묵 = 유휴 초), 손이 키보드에 있는 동안 알림을 붙들고, 스트림을 한 문단으로 찍고, UTF-8 키를 디코드하고, 모든 종료 경로에서 터미널을 복원하고, 제3자 글을 sanitize 함 | `test_tui.*`, `test_invariants.test_presence_has_a_producer` |
| SIGTERM 과 Ctrl-C 둘 다 `close()` 하나로 끝남(가짜 stdio 서버 프로세스가 사라짐); 신호 턴 도중의 `say` 는 자기 턴을 얻고, 사용자 턴 도중엔 여전히 steer | `test_daemon.SigtermTest`, `test_say_during_signal_turn_starts_its_own_turn`, `test_daemon_say_during_turn_steers` |
| `merge_json_file` 은 다른 키를 전부 지키고, 먼저 백업하고, 없는 파일은 만들고, 읽어서 다르면 raise | `test_installer.test_merge_json_file_*` |
| Anthropic 어댑터는 녹화 SSE 에서 같은 튜플을 냄(text, `input_json_delta` 로 조립한 tool_use, usage), 이력을 가장자리에서 변환, 429/402 는 `blocked`; `api` 없는 설정은 전과 똑같이 `openai` 로 풀림; 어느 테스트도 네트워크 소켓을 열지 않음 | `test_llm_anthropic.*` |
| `init.py` ≤ 300, `tui.py` ≤ 400, `tui_secret.py`(`/secret` 의 가린 입력, D64) ≤ 150; README.md 와 README.ko.md 의 제목 수가 같음 | `test_invariants.test_init_module_under_300_lines`, `test_tui_module_under_400_lines`, `test_tui_secret_module_under_150_lines`, `test_readme_pair_has_equal_heading_counts` |
| `memory` 는 옵트인 툴 origin, 정확히 `search_memory, read_memory, read_ledger, edit_memory`; 소켓이 `signal memory ...` 를 거부함; `memory.py` 와 `daemon.py` 는 780줄 이하(배분 600 의 1.3배 선) | `test_memory.test_tools_for_origin_memory`, `test_daemon.MemoryDaemonTest.test_socket_refuses_signal_memory`, `test_invariants.test_memory_module_under_780_lines`, `test_invariants.test_daemon_module_under_780_lines` |

## 13. 세는 법

```bash
python3 -m unittest discover -s tests            # 전체 테스트, 네트워크 없음 (440)
wc -l pathfinder/*.py pathfinder/llm/*.py pathfinder/senses/*.py   # 코어; 배분: ledger 400 ·
                                                  # state 600 · daemon 600(오늘은 756, 780 이 하드
                                                  # 라인) · loop 400 · broker+policy 350 ·
                                                  # exec 300 · tools 300 · clock+signals+rules 450 ·
                                                  # deliver 250 · jobs 350 · memory 600(오늘 686, 780 이 하드 라인) ·
                                                  # 감각 파일 150 · catalog 300 · installer 700
                                                  # init 300(구획 7); 코어 상한 7,000(D34) — 모듈별 1.3배 선이 게이트
wc -l pathfinder/tui.py pathfinder/tui_secret.py pathfinder/llm/anthropic.py  # 코어 밖: tui ≤ 400, tui_secret ≤ 150 (D64), 실험 어댑터는 llm/ 의 600 안
wc -l pathfinder/mcp/*.py                         # 이식한 MCP 클라이언트: 어댑터 예산 1,858, 코어 밖
python3 bench/m5_install.py                       # m5: 임시 HOME 에 레지스트리에서 설치(네트워크·npx, 모델 크레딧 0)
python3 scripts/scan_personal.py --all           # 추적 파일 전체 개인정보 스캔
```

## 14. 돌리는 법

```bash
pathfinder init                                   # 온보딩: 검사 · 모델 선택 · 감각 · 훅 · launchd(구획 7)
pathfinder service start|stop|restart|status      # launchd 에이전트; stop = bootout(KeepAlive 가 되살릴 수 없음)
pathfinder tui                                    # 화면 하나: 입력 · 읽기 · /approve ID · /deny ID · /approvals · /jobs · + 또는 - · /quit; Ctrl-C 는 중단, 두 번이면 종료
python3 -m pathfinder config set model provider:model-name
python3 -m pathfinder daemon
python3 -m pathfinder say "안녕"                  # 답이 흐르는 중의 두 번째 `say` 는 steer
python3 -m pathfinder listen                      # 붙어서 델타와 신호 알림을 출력
python3 -m pathfinder approvals                   # 승인 대기 목록, state.db 에서 읽음
python3 -m pathfinder approve ID | deny ID        # 게이트에 걸린 턴을 재개하고 답을 출력
python3 -m pathfinder interrupt                   # 최근 세션의 도는 턴을 멈춤
python3 -m pathfinder config set grants '{"write_file": "allow"}'
python3 -m pathfinder config set senses '{"schedule": {}, "shell": {}, "calendar": {}, "reminders": {}}'
python3 -m pathfinder signal shell '{"cmd": "make test", "exit": 1, "seconds": 3, "cwd": "/tmp"}'
python3 -m pathfinder config set senses '{"mail": {"host": "imap.example.com", "user": "you@example.com"}}'
pathfinder secret mail                            # mail 을 켠 뒤 한 번 (화면에서는 /secret mail)
python3 -m pathfinder react up                    # 최근 전달에 엄지척
python3 -m pathfinder rules                       # RULES.md 의 경로를 출력
python3 -m pathfinder reindex                     # 데몬이 도는 중에는 거부
python3 -m pathfinder jobs                        # id, state, started, goal - state.db 에서
python3 -m pathfinder job stop ID                 # 그 작업의 control.interrupt 를 세움
python3 -m pathfinder job steer ID "X 에 집중해"    # 작업의 다음 모델 호출 전에 닿음
python3 -m pathfinder config set embed_model provider:embedding-model-name   # 비우면 벡터 없음
python3 -m pathfinder memory                      # 커서, passes/read/applied, 대기, 판정
python3 -m pathfinder memory run                   # 지금 통합 패스를 강제로 돌림
python3 -m pathfinder memory undo                  # 최근 memory_apply 를 그 역으로 되돌림
```

## 15. 설치와 화면 (구획 7)

- **`pathfinder init`**(`init.py`, ≤ 300줄)은 검사를 순서대로 돌고 첫 하드 실패에서 정확한 해결 줄을
  찍고 멈춘다: Python ≥ 3.11; Ollama 가 `GET <providers.ollama.base_url>/models` 에 답함(id 를 번호
  매겨 보이고, 선택은 `config.set("model", "ollama:<id>")` 을 거친다; 목록에 임베딩 모델이 있으면
  `embed_model` 로 제안; 코드에도 README 에도 모델 이름은 없다 — 목록은 기계에서 온다); `npx`(경고만);
  홈 디렉터리(0700; `RULES.md`/`USER.md` 는 데몬의 첫 시작 몫); 비밀 없는 감각을 하나씩 예/아니오로
  묻고 `config.set` 한 번으로 `senses` 에 병합(mail · calendar 는 묻지 않고 안내만 — 비밀번호나 권한
  창이 필요하고 `init` 은 어느 쪽에도 막히면 안 된다); 셸 훅(스니펫을 `<home>/hooks/pathfinder.zsh`
  에, 백업 뒤 `~/.zshrc` 에 가드된 한 줄 덧붙임 — 파일 끝에 줄바꿈이 없으면 먼저 하나; 스니펫은
  `zsh/datetime` 을 싣고 `add-zsh-hook` 으로 사용자 훅 옆에 붙는다); Claude Code 훅(`config.merge_json_file`, D60 으로
  `~/.claude/settings.json` 에 두 항목 병합); launchd 에이전트(`plistlib` 로 쓰고 `launchctl bootstrap
  gui/<uid> <plist>`, 그 뒤 소켓을 최대 10초 기다림). 모든 단계가 멱등이다: 두 번째 실행은 파생 파일을
  바꾸지 않는다. 답은 `input()` 으로 받아 테스트가 파이프로 넣고, `launchctl` 은 주입 가능한 함수 하나다.
- **`pathfinder service start|stop|restart|status`**: `bootstrap` / `bootout gui/<uid>/pathfinder.daemon`
  / `kickstart -k` / `print` 를 한 줄 + 소켓이 답하는지로 줄인 것. `stop` 은 신호가 아니라 `bootout`:
  `KeepAlive` 가 걸린 에이전트는 맨 SIGTERM 에 되살아난다.
- **launchd 아래의 데몬**(§7): SIGTERM 과 Ctrl-C 둘 다 `close()` 에 닿는다; 로그는 `<home>/daemon.log`;
  `WorkingDirectory` 는 패키지의 부모라 클론에서도 `-m pathfinder` 가 풀린다(launchd 의 cwd 는 `/`).
- **`pathfinder tui`**(`tui.py`, ≤ 400줄, stdlib, `mcp/` 처럼 코어 밖; D35): 한 자리 동안 붙어 있는
  클라이언트 하나. 연결하고 `attach {cwd}` 를 보내고(`attach` 감각의 맥락 출처 — 전에는 일회성 `say`
  만 보냈다), 터미널을 cbreak + `ISIG` 꺼짐으로 두고(Ctrl-C 가 바이트로 와서 직접 처리한다: 턴이 도는
  중이면 `interrupt`, 아니면 종료), `selectors` 로 stdin 과 소켓을 다중화한다. 화면은 터미널 자신의
  스크롤백: 스트림은 `turn.start` 와 `turn.end` 사이에 한 문단으로 찍히고, 그 밖의 것은 위에 찍힌 뒤
  입력줄(`\r\033[K` + 프롬프트 + 버퍼)이 다시 그려진다; 키가 들어오는 중에 온 `notice` 는 1초 조용해질
  때까지 기다린다. 키는 UTF-8 로 디코드한다(한글 한 음절은 3바이트). Enter → `say`; `+`/`-` 만 → `react`;
  `/approve ID` · `/deny ID` · `/approvals` · `/jobs`(CLI 자신의 목록, `state.db` 에서) · `/quit`; 툴이
  끊기지 않는 턴에서 두 번째 Ctrl-C 는 종료; 화살표 키는 버린다. 스트림 중에는 키가 조용히 버퍼에
  쌓이고 다른 줄은 제 줄을 차지하므로 흐르는 글이 지워지지 않는다. **`presence` 의
  유일한 실제 생산자다**: 타이핑 중 `{idle_s: 0}` 을 1초에 최대 한 번, 침묵 30초마다 `{idle_s: n}` —
  전달 상태기계의 타이핑 · 자리비움 분기(§8)는 구획 3부터 6까지 라이브 출처가 없었다. `tui.render(msg)`
  가 렌더러 하나; `listen` 도 그것으로 찍는다. 고정 문구는 표 하나에 한국어 · 영어 나란히(D6, 설정
  없음); 알림 글과 작업 목표는 `mcp.protocol.sanitize`(`tools.clean`)를 거친다. 승인 줄 — `gate` · `job.gate`
  줄, `say` 의 게이트 줄, `pathfinder approvals`, `pathfinder tools` 가 찍는 이름 — 은 대신 `tools.visible`
  을 거친다: 보이는 것이 실행되는 것이므로 숨은 문자는 버리지 않고 이스케이프(`\u200b`)로 보인다.
  터미널은 모든 경로에서 `finally` 로 복원된다.
- **`llm/anthropic.py`**(§4; D2, D37): 실험적, 녹화 테스트만, 라이브로 돌리지 않음(D7).
  `providers.<name>.api = "anthropic"` 이 고른다; 기존 설정은 전부 `openai` 로 풀린다.
- **README.md / README.ko.md** 는 10분 경로(Homebrew 준비물, Ollama 계정, `git clone` + `pipx install .` —
  Homebrew 의 Python 은 맨 `pip install` 을 거부한다, PEP 668 —,
  `pathfinder init`, `pathfinder tui`)를 담고 모델 이름을 적지 않는다; 불변조건 테스트가 두 파일의 제목
  수를 맞춘다. 이 구획의 출구 실측은 `m8` — 새 macOS 계정의 사람이 README 만으로 10분 안에 첫 대화에
  닿는가 — HISTORY 에 적히고 구획 8 을 게이트한다.


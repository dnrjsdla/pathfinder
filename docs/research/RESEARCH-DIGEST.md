# RESEARCH-DIGEST — PATHFINDER 구조 고안 (2026-09-21)

## 기준 숫자 (내 눈으로, 1단)
- Mark.5: Python 14,028줄(코어 7,367 · 어댑터 3,869 · 클라이언트 tui 517/telegram 382), 커밋 182, 최근 30일 +32,048/−34,007줄, 테스트 622, 환경변수 28, 논리 표 4.
- 구조: 커널(brain/+gateway/) + 텍스트 유저랜드(vault). 트리거 넷(user/signal/cron/gate 재개) → ReactLoop 하나(코드 모드: 다음 수 = 파이썬 스크립트) → 무자격 실행기(Seatbelt) → fd3 → Broker → policy.decide(툴×origin).
- 감각 4(Mail/Activity/Wake/Calendar), 판정 2단(rules.md 닫힌 어휘 9 → 모델), 전달 상태기계(presence/typing), 낮(5턴 창 증분)/밤(기억→승격→절차→하네스→코드 5단), gate.py 결정론, 등급 auto/inbox/pr/frozen.
- 지원서: 비서=오픈소스 무료, 서버(선검증 도구 목록·게이트웨이)=비공개·팀원 담당. 1R 시제품 = MCP 레지스트리 1곳 → 검증 목록 → 비서가 맥락 요약해 요청 → 후보 → 제안 → 승인 1회 → 설치. 1차 지표 = 베타 10명 2주 1인당 신규 도구 부착 ≥ 주 1건. 서버로는 "일의 내용이 아니라 결과만"(제안/승인/거절/설치 실패). 다른 비서(OpenClaw/Hermes)도 붙는 연결 규격 공개. 검사 로그 보존 + 면책 고지 1R 포함. GPL/AGPL/비상업/무라이선스 제외, 설치 시 원본에서 받아 해시 대조.

## 4. 에이전트 DB (sonnet, 도착)
- SQLite+FTS5+sqlite-vec+JSONL 원본+markdown vault = 2026 표준 관행("log is the agent"). sqlite-vec 는 아직 v1 전·brute-force 기본이나 10^3~10^5 문서엔 충분.
- KuzuDB 2025-10-10 아카이브(Apple acqui-hire) → 제외. DuckDB vss 실험적(인덱스 전부 RAM). pglite 는 JS 중심 → 부적합. Turso/libSQL 네이티브 벡터(DiskANN)·AgentFS 는 신기술이나 Python·kiwipiepy 토크나이저 통합 미확인 → 보류.
- DB 브랜칭(Neon/Dolt)은 개인 데몬엔 hype(BranchBench 2026-05: "어떤 시스템도 에이전트 워크로드 제대로 지원 못함").
- 하이브리드 융합은 RRF 가 표준. Anthropic Contextual Retrieval: 실패율 −35%(임베딩) / −49%(+BM25) / −67%(+rerank). 로컬 리랭커 bge-reranker-v2-m3(-ko), Qwen3-Reranker-0.6B.
- 임베딩: BGE-M3(0.6B) 또는 Qwen3-Embedding-0.6B 가 한·영 균형. **Ollama 의존 제거 = fastembed(ONNX, pip 만)** → 비개발자 설치 마찰 감소.
- 그래프: 단순 사실 조회는 청크 60.9 vs 그래프 60.1 동률, 멀티홉만 +35%. LLM 자동추출 그래프는 유지비 큼 → `[[위키링크]]`를 경량 에지(entities/edges 표 2개)로.
- 상태 스키마: 이벤트 로그 + 주기 스냅샷, runs/approvals 분리, provenance 칸(origin, source_trust, valid_from/until — Graphiti bi-temporal 차용). durable queue 는 DBOS Transact(SQLite 지원) 또는 `BEGIN IMMEDIATE` 자작.
- 텔레메트리: opt-in + **allowlist 프로젝션**(도구명·열거형 상태·시각·OS 만). SQLCipher 는 wheel 마찰 미확인. Litestream v0.5.17 버전 고정.
- 권고: A(현행 SQLite 삼중 구조 계승 + fastembed + RRF + provenance) → 필요 시 C(위키링크 경량 그래프) → B(Turso) 보류.

## 2. 능동형/이벤트 구동 하네스 패턴 (sonnet, 도착)
- LangChain ambient agents: 인터럽트 3분류 **Notify / Question / Review** + Agent Inbox(큐형 UI). LangGraph `interrupt()` = 상태를 checkpointer 에 재우고 그 노드에서 재개(대기 중 비용 0).
- Claude Code 모델: hooks(결정론, ~30 이벤트) · background task 완료 통지가 에이전트를 재호출 · Monitor(event-driven push, 200ms 배치, 매칭 없으면 비용 0) · /loop(세션당 50개·3일 만료) · Channels(MCP 로 Telegram 등 양방향). Codex `turn/steer` = 턴 중 주입(다음 tool 경계에서 반영). Cursor = fire-and-forget + 완료 알림. → 두 축: **mid-turn steering vs 완료 후 async notify**.
- Proactive Agent(Tsinghua, ICLR 2025): ProactiveBench 6,790 이벤트, reward model F1 0.918, 파인튜닝 모델 F1 66.47%. **PROBE: GPT-5/Opus 4.1 도 end-to-end 40%** → LLM 단독 개입 판단은 근거 약함, 규칙 게이팅 병행.
- 타이밍: Iqbal&Bailey CHI08 — task breakpoint 에서 전달하면 반응시간·불쾌감 감소(20년 누적). CHI 2025 "Assistance or Disruption" — dismiss/revert 를 disruption 으로 측정.
- 알림 설계: Rules Engine(판단) / Delivery Worker(전달) 분리, dedupe window(예 5분). 규칙→소형→대형 모델 cascade 의 1차 프로덕션 사례는 미확인.
- Durable: 단일 사용자 데몬은 SQLite append-only journal + WAL 로 충분(Morling "Durable Execution Engine with SQLite", DBOS SQLite 백엔드). 최소 셋: pending approval 표 · 툴콜 idempotency key · 턴 경계 상태 커밋.
- 멀티모델: Anthropic Research = Opus 리드 + Sonnet 서브, 단일 Opus 대비 +90.2%(토큰 ~15배는 미확인 표기). 서브에이전트는 fresh context, 최종 메시지만 반환. 실패 모드: 중복 작업 · 컨텍스트 유실 · 순차 작업에 병렬 적용.
- Anthropic 가이던스: Building Effective Agents(단순한 것부터), Context engineering(write/select/compress/isolate), Long-running harness(2025-11-26: progress 파일·git 로그·feature list JSON 핸드오프, 테스트 삭제 금지).
- **공백**: "워커 완료 통지를 인터럽트로 낼지 Notify 큐로 낼지"와 "신호 도착 시 스스로 대화 재개하는 상시 루프"의 검증된 레퍼런스 없음 → PATHFINDER 자체 측정(false-alarm, 수용률)으로 채워야 함.

## 1. OpenClaw · Hermes Agent (sonnet, 도착)
- OpenClaw(TS+Swift, MIT, 스타 38만+): 단일 Gateway(WS+HTTP) = control plane, CLI/TUI 는 클라이언트. 턴은 텍스트 전용 응답에서 종료. **세션별 lane**(세션당 run 하나) + 큐 모드 4종 `steer`(기본, 실행 중 턴에 주입)/`followup`/`collect`/`steer-backlog`. 재시작 시 큐잉 입력 유실(Issue #135880).
- OpenClaw 능동성: `HEARTBEAT.md`, `notify:false` 면 침묵하되 다음 턴용 bounded context 로 기억. 문서가 "**silence criteria 없으면 매 사이클 보고해 노이즈**" 명시. cheap checks first. **이벤트 턴 최소 30초 간격 + 60초 5회 flood guard**.
- OpenClaw 사고: ClawHub 무심사 → 2026-02 **341/2,857 악성**(ClawHavoc, AMOS infostealer), 재스캔 824/10,700+. CVE-2026-25253(WS hijack→RCE, CVSS 8.8), CVE-2026-25593/44118(**로컬 WS 무인증 → config/cron 장악**), CVE-2026-32015(이름 기반 allowlist PATH 우회), ClawJacked(웹페이지 숨은 지시 → 완전 탈취), 평문 시크릿. 2026-09 git-config hijack(core.fsmonitor/hooksPath)은 업계 전반.
- Hermes(Python, 테스트 ~25,000): **AIAgent 클래스 하나가 CLI/Gateway/ACP/batch/API 전부 서빙**. 트리거 = 유저/게이트웨이(25+ 어댑터)/cron(jobs.json). mid-flight 취소 가능. **stable/context/volatile 3층 프롬프트**, SOUL.md 슬롯 #1. SQLite+FTS5, 임계값 트리거 압축. 서브에이전트 **하드 캡 90턴 + 공유 예산**. 샌드박스 백엔드 7종. 턴 종료 후 백그라운드 self-improvement 리뷰 → 스킬 md.
- Hermes 사고: CVE-2026-9366(`_scan_context_content`), CVE-2026-10223(`_scan_memory_content` → RCE) — **sanitize 함수 자체가 공격 표면**, 벤더 무응답. "9 CVEs in 4 days"(CSA 2026-05-04).
- 채택: 3층 프롬프트 · silence criteria 강제 · 숫자 rate limit · lane+큐 모드 · 서브에이전트 공유 예산 · core loop 하나 · egress 차단 기본 · 설치 승인 게이트. 회피: 오픈 마켓 무심사 · 평문 시크릿(→Keychain) · 로컬 소켓 무인증 · 이름 기반 exec allowlist(→절대경로+해시) · `.git/config` 무검사.

## 3. LLM Wiki · 메모리 (sonnet, 도착)
- Karpathy LLM Wiki(2026-04, gist 442a6bf5…): 3계층 raw(불변) / wiki(LLM 생성, 상호 링크) / schema(CLAUDE.md 류). 연산 **ingest**(요약→index.md→관련 10~15쪽 연쇄 수정→log.md) · **query**(좋은 답은 다시 페이지로) · **lint**(모순·stale·고아·누락 링크). RAG 비판 = "매번 재발견". 규모 ~소스 100·페이지 수백은 임베딩 없이 동작. **모순 처리는 LLM 판단에 위임 — Mark.5 의 결정론 게이트+inbox 보다 약함**. 워딩은 미확인(원문 직접 대조 필요).
- 1개월 회고(rdworld): 760쪽, "**유지보수 시간 ≈ 절약 시간**", 스키마+lint 가 품질 게이트.
- Letta: sleep-time agent(non-blocking, arXiv 2504.13171), memory block, **Context Repositories = git-backed memory + 서브에이전트별 worktree + MemFS(파일 트리가 프롬프트에 상주)**. Claude "Dreaming"(Managed Agents, research preview): 야간 리뷰·prune·병합·모순 해소, 수치 미확인. Claude Code auto-memory = MEMORY.md 인덱스 + 토픽 파일.
- ACE(arXiv 2510.04618): Generator→Reflector→Curator, **delta 만 append**. context collapse 실측 **18,282→122 토큰, 66.7%→57.1%**. agent +10.6%, 도메인 +8.6%. brevity bias.
- 구조화 메모리 벤치마크는 신뢰 위기: Mem0 LoCoMo 94.4% vs LongMemEval 49%, Zep↔Mem0 상호 반박. LongMemEval-S 에서 verbatim RAG 47.2% vs full-context 49.8% 유의차 없음. grep+read 에이전트가 정교한 selector 를 이긴 사례. → "본인 데이터로 직접 측정".
- Manus: 파일시스템 = 외부 메모리, **recoverable compression**(본문 버리고 경로/URL 만, 100:1), todo.md 로 attention 조작.
- Memory poisoning = 지속형 prompt injection(arXiv 2606.04329, Unit42). 방어 4지점: write-time admission · provenance binding · retrieval filtering · post-hoc 탐지. diff 기반 승인 + 원클릭 revert 가 업계 수렴 패턴 = **Mark.5 와 같은 방향**.
- 분업: 싼 모델=추출/태깅, 강한 모델=승격/병합/모순. mem0: 3-call → single-pass ADD-only 로 write-time 호출 −60~70%(벤더 수치).
- 권고: 유지 = vault 1급 · append-only 원본 · 낮/밤 분업 · 결정론 게이트+inbox. 바꿈 = wiki lint 게이트(필수 인용·허용 헤딩) · vault git 버저닝 검토 · single-pass 추출. 추가 = JSONL 에 provenance 칸 명시 · 의심 메모리 review queue 라우팅 · recoverable compression.

## 5. MCP 레지스트리 · 설치 · 검증 (sonnet, 도착)
- 공식 레지스트리: 무인증 read-only REST `GET /v0.1/servers`(cursor, `updated_since`), `/versions/{version|latest}`. server.json(스키마 2025-12-11): `packages[]` registryType npm/pypi/cargo/nuget/oci/**mcpb(fileSha256)**, `runtimeHint` npx/uvx/dnx, `environmentVariables{name,isRequired,isSecret}`, `remotes[]`. 검증하는 것 = namespace 소유권뿐. preview(데이터 리셋 가능), rate limit 수치 미확인. subregistry 는 `_meta` 자유형식 — **검증 결과 표준 필드 없음**.
- 스펙 **2026-07-28 실재**: stateless 코어(세션·initialize 핸드셰이크 제거), `Mcp-Method`/`Mcp-Name` 헤더, list 응답 `ttlMs`/`cacheScope`, DCR→CIMD. 2025-11-25: Tasks(실험), URL-mode elicitation(비밀을 채팅창에 안 붙이게). tool annotations(readOnly/destructive/idempotent/openWorld)는 **힌트지 보장 아님**. Python `mcp` **2.2.0**(2026-09-07), 구코드는 `<2` 핀.
- 설치: MCPB(zip+manifest), config 위치 클라이언트마다 다름(Claude Desktop `~/Library/Application Support/Claude/claude_desktop_config.json`, Claude Code `~/.claude.json`+`.mcp.json`, Cursor `~/.cursor/mcp.json`). Smithery "verified" 는 자체보고(100개 중 22개 이슈). Docker MCP Catalog 270+ 서명·SBOM.
- 설치기 경우의 수: 런타임 부재(node/uv/docker) · nvm/pyenv 로 PATH 에 npx/uvx 없음 · secret(Keychain, 평문 금지) · 기존 config 비파괴 병합 · 버전+해시 불일치 시 중단 · 실패 시 롤백 · 업데이트 시 재검증 · 제거 흐름.
- 보안 도구: Snyk agent-scan(구 mcp-scan, v0.4.13) · Cisco mcp-scanner(YARA+LLM+API) · **Trail of Bits mcp-context-protector(TOFU 해시 핀 — Mark.5 mcp/pins.py 와 같은 기제)** · OWASP MCP Top 10. 2026-01~02 MCP CVE 30+건.
- 사고: ToxicSkills — 스킬 36% 결함, **악성의 21%가 런타임 원격 fetch → 정적 스캔 우회**. GitSpawn(2026-09-01, CVE-2026-72718): `.git` 설정으로 모델 응답 전에 실행. Plugin4Shell(2026-09): 커밋 해시 핀을 브랜치명으로 우회.
- 한계: 정적 스캔은 원격 페이로드 못 잡고, 샌드박스 시험은 지연·조건부 악성 못 잡음 → **시험 + 해시 핀 병행**.
- 샌드박스: Anthropic **sandbox-runtime(srt)** = macOS Seatbelt 동적 프로파일 + HTTP/SOCKS 프록시 도메인 allowlist, Linux bubblewrap.
- 맥락→도구 매칭 프라이버시 4안: taxonomy 태그(유출 최소) / coarse 클러스터 ID(임베딩 원본은 inversion 위험) / **카탈로그 내려받아 온디바이스 매칭**(프라이버시 최고, 신선도 위험) / 쿼리 시점 서버 매칭(최악). 권고 하이브리드: coarse 태그만 보내 후보 받고 최종 순위는 로컬.
- 공백: Tool Search 는 **설치된** 툴 검색(regex 56% / BM25 64%)일 뿐. 미설치 도구의 맥락 기반 선제 추천 + 검증 + 설치를 한 흐름으로 묶은 시스템 없음 = PATHFINDER 자리.
- 연결 규격 필드 초안: name/version/source_registry · verification{license(SPDX+실탐지 일치), malicious_scan{engines,result,scanned_at}, sandbox_trial{ran_at,network_calls,file_writes,verdict}, maintenance{last_commit_at,issues,stars,namespace_verified}, description_hash, trust_tier} · install{runtime, pinned version+hash, env[], args, permissions 요약} · 재검증 트리거.
- 1R 최소(제안): 레지스트리 1곳 · npm/pypi · 설치 대상 클라이언트 1개 · 표시 = license + last-commit-age + 샌드박스 네트워크 호출 목록 · npx 전제 · secret 1~2개 · 키워드 coarse 태그 · 해시 저장+변경 경고.

## 7. Claude Code 팀 운영 · Agent SDK (fable, 도착) — 로컬 Claude Code 2.1.278
- subagent frontmatter: name, description, tools, disallowedTools, **model**(sonnet/opus/haiku/fable/full ID/inherit), permissionMode, maxTurns, skills, mcpServers, hooks, **memory**(user/project/local), background, omitClaudeMd, **effort**(low~max), **isolation: worktree**, color, initialPrompt. model 우선순위: 호출 파라미터 > frontmatter > `CLAUDE_CODE_SUBAGENT_MODEL` > main. Sonnet 5/Opus 5/Fable 5.1 기본 effort 가 이미 high → `effort: high` 는 명시 고정. `/tasks` 에서 실제 model·effort 확인. `fable` alias = Fable 5.1.
- Agent teams: experimental(`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`). teammate effort 는 lead 상속, **plan 승인을 lead 가 자동으로 함(사람 gate 아님)**, worktree 격리 없음, resume 불가 → 기본 운영 부적합.
- 수단: subagents(일상) · workflows(`.claude/workflows/`, 동시 16, 실행 중 사용자 입력 불가 → 단계마다 별도 workflow) · worktrees(**git 필수**) · /goal · /loop(7일 만료) · Routines(cloud, 최소 1h).
- 운영: 결정은 `docs/decisions/` ADR 이 주 저장소, CLAUDE.md 는 포인터. 테스트 강제 = Stop hook exit 2(8회 cap). **secret scan 은 git `pre-commit` 이 확실**(Claude hook `if` 는 best-effort). settings.json permissions allow/deny 는 사용자 승인 사항.
- 비용: API 단가 $/MTok in/out — Sonnet 5 2/10 · Opus 5 5/25 · Fable 5.1 10/50 · Haiku 4.5 1/5(**retire ≥ 2026-10-15 → 모델 ID 하드코딩 금지**). Fable 은 플랜에 따라 usage credits 과금, `-p`/SDK 에선 묻지 않고 과금.
- 권고 팀: architect(fable, plan, 코드 수정 없음) · researcher(sonnet) · implementer(sonnet, worktree, maxTurns 60) · senior-impl(opus, worktree) · reviewer(opus, read-only, memory: project) · qa(sonnet). lead 모델 3안: opus / fable / opusplan.
- Agent SDK(Python `claude-agent-sdk` 0.2.157): `ClaudeSDKClient` = 상주 multi-turn + interrupt, streaming input 은 **큐에 쌓여 순차 처리**(mid-turn 삽입 semantics 미확인), hooks, `@tool` in-process MCP, `can_use_tool`, sessions resume/fork(JSONL), sandbox, `setting_sources=[]` 로 사용자 ~/.claude 차단 필수. Node 불필요, **CLI binary 가 wheel 에 번들(93~103MB), 세션당 subprocess, 권장 1GiB RAM**.
- 인증 정책 원문: "Anthropic does not permit third-party developers to offer Claude.ai login … or to route requests through Free, Pro, or Max plan credentials on behalf of their users." → **배포 제품의 최종 사용자는 API key/cloud credential 만**. "사용자가 로그인해 둔 unmodified Claude Code 를 SDK 가 spawn" 은 회색지대(미확인). 브랜딩: "Claude Code" 명칭 금지, "Powered by Claude" 허용.
- lock-in: "doesn't support routing Claude Code to non-Claude models through any gateway" → **멀티 provider 면 SDK 로 표현 불가**.
- 자체 하네스용 API: prompt caching(자동/explicit 4 breakpoint, TTL 5m 1.25x·1h 2x, read 0.1x, 최소 512~4096 tok, `max_tokens:0` pre-warm) · tool runner(승인 gate 는 manual loop 권장) · programmatic tool calling(Haiku 미지원) · tool search · **memory tool(client-side)** · context editing · compaction(`compact-2026-01-12`) · **Batch 50%**(야간 통합에 적합) · Managed Agents 는 Anthropic 호스팅이라 로컬 상주에 부적합(추론).
- 되돌림 비용 최소화: **도메인 툴을 MCP 서버(in-process/stdio)로 작성**하면 SDK↔자체 하네스 양방향 이식.
- 실측 제안: SDK 설치 크기·기동 시간 / 상주 client RSS / 이벤트 주입 큐잉 지연·interrupt / API key 온보딩 마찰.

## 6. Mark.5 실측 진단 (opus, 도착 — 핵심 둘은 직접 재확인함)
- 25일 실사용: 판정 2,239 → **발화 50(2.2%, 하루 2회)**, 그중 달력/미리알림 근거 23. long_focus 1,274건(56.9%) → 발화 0 = 순수 소음. 1단 규칙이 74%(1,661 defer)를 비용 0 으로 처리. 2단 모델 판정 115건 중 **failed 34(29.6%)**. react 라벨 6건(good 5/bad 1) → 학습 입력 빈약.
- 자가학습: 밤 5단계 중 4단계가 **24/24일 산출 0**. 위키 .md 2장, 스킬 0. 원인 = `store.session_path`(열린 날 폴더) × `night._session_files`(day, day-1 만) × `recent_session`(이어붙기) 의 **파티션 키 이중 정의 → 자기 유지형 교착**. 테스트 566개가 못 잡음(경계 결함). [재확인: 마지막 폴더 2026-09-09, 파일은 09-21 까지 218KB]
- 장부 12개 중 4개가 같은 사실을 적고 2개(events·wiki 표 13개)는 08-27/28 이후 죽음. 판정 자리 9, 독립 시계 4+시간창 8, 설정 자리 11(`capabilities.senses` 읽는 코드 0, `gateway/store.bind` 호출자 0). run_steps 기록 71% 누락. worker.log 는 pytest 산출물로 오염.
- ReAct steps: 1회 60%, 최대 7 → MAX_STEPS 40 은 5.7배 과잉.
- 확장 비용: MCP 서버 0파일 · 네이티브 툴 1 · provider 1 / **감각 3~4파일 · 클라이언트 표면 400~520줄 중복** → 늘려야 할 축(신호)이 비싼 쪽.
- 갭: 턴 중 주입 불가(queue 모드뿐, steer 미구현) · interrupt 는 프로토콜만 있고 서버가 버림 · 스트리밍 없음(provider 4개 동기 호출) · 서브에이전트/백그라운드 작업 없음(roles 5→3 축소는 **반대 방향 철학**) · 도구 카탈로그/제안/설치 0줄(MCP 설치 = 손으로 mcp.json 편집) · macOS 호출지점 55/12파일 · 한글 포함 줄 26.7% · 기본 모델이 ollama · IMAP 비밀번호 평문 .env.
- 위생: 추적 파일 내 이메일/키/실명/절대경로 0건(모범적). **커밋 182/182 author·committer = 기관 이메일**, 전역 git 설정도 동일 [재확인].
- 이식 예산: 그대로 ≈2,250줄(`brain/mcp/` 1,858 · `policy.py` 228 · `react.py`+`broker.py` · gate 등급표 패턴) + 고쳐서 ≈1,200줄 생존(`gateway/` 스트리밍 계약 재작성 · `rules.py` · `index.py`) = **≈3,450줄(28%)**. 포크 시 다시 쓸 코어 = daemon 638+turn 452+react 153+gateway 1,183 = 2,426줄.
- 승계할 불변조건: 파일이 진실·SQLite 는 색인(빈 DB 에서 reindex 복원) · 생성(night)/평가(gate 결정론) 분리 · origin×툴 정책표 · 신호 봉투+검색 블록 위치 실측(주입 2회 중 1회 → 0) · 핀 기반 rug pull · ARCHITECTURE §9 불변조건 표/§10 세는 법.
- 교훈: 파티션 키를 두 곳에서 정의하지 말 것. **"산출 0"이 정상과 구분되지 않는 파이프라인에는 연속 N일 0 경보**가 구조에 있어야 함.

## 근본원인 한 문장 (3단)
"쓰이는지 재는 자리 없이 장부와 단계를 먼저 늘렸다 — 그래서 장부 12개 중 4개가 죽고 자가학습 4단계가 24일간 산출 0 인데 아무도 몰랐다." (CLAUDE.md §5.4 "작동하는 전체" 위반: 돌지만 산출이 없는 것은 '작동'이 아니다.) → PATHFINDER 의 모든 구조 제안은 **① 장부 하나 ② 부품마다 산출 카운터+경보** 에 걸러서 본다.

## 6-b. Mark.5 진단 정정·보충 (opus 후속, 정정 건은 직접 재확인)
- **정정**: 추적 파일 위생 "0건"은 틀림 — 추적 문서 1곳에 소유자 계정 핸들 1건(백틱 안의 맨 owner/repo 슬러그) [재확인]. 공개 전 처리 대상 둘 = 커밋 메타데이터 이메일 + 이 핸들. → PATHFINDER 개인정보 스캔 룰에 **맨 owner/repo 슬러그** 추가.
- 다중 LLM: `roles.py` 가 반대한 것은 "한 메시지 리스트를 여러 모델이 쪼개 갖는 것"이고, `docs/BRAIN-PLAN.html` §7·§9 는 "다른 lane 에서 돌고 요약만 speak 로 돌아오는 /bg 위임 · origin task · task.done" 을 **설계만 하고 미구현**. → 철학 충돌이 아니라 미구현 설계 승계. 잔존물 = `Origin.DAEMON`(생산자 없음).
- `prepare` 3중 드리프트: HISTORY 는 폐기, ARCHITECTURE·`rules.py:28` 는 유지, `daemon.py:228` 은 model 과 합류, 일지에는 71건이 오늘까지 적힘 [재확인: 71] → **버린 어휘를 장부에서 안 빼면 학습 입력이 오염**.
- 구획 7(실사용 판정)은 재지 않고 main 병합(2026-09-09), "2주 뒤 판단"은 오늘 지났고 기록 없음. D3 의 뒤집는 조건("한 달간 코드 제안이 bench 통과 0 이면 코드 경로 닫음")도 충족됐으나 안 닫음. D14·15·17·18·19 원문은 리포 밖 세션 아티팩트 → **결정 근거는 코드와 같은 리포에**.
- 이식 자산 추가: `bench/injection.py`(남의 지시를 따른 계획 **0/20**, 블록 뒤 배치 시 누설 1/20 → 앞 배치 0/10) = 첫날부터 회귀 게이트. `bench/proactive.py` 1단 규칙 **precision 1.00 · recall 0.61 · 결정률 70%** → 모델 계층보다 **규칙 계층을 넓히는 것이 먼저**.
- 불변: 새 레포 + 선별 이식 권고와 이식 예산(≈3,450줄, 28%)은 그대로. 정정 건은 포크 비용을 더 올림.

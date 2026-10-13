# PATHFINDER

[English](README.md)

AI로 일하는 사람을 위한 상주형 능동 비서. Mac 위에서 백그라운드 데몬으로 살면서, 말할 가치가 있는
일이 생기면 먼저 말을 겁니다(실패한 빌드, 답해야 할 메일, 지금 연 프로젝트에 맞는 도구). 사용자가 한
말은 사람이 읽을 수 있는 Markdown 파일에 기억하고, 하는 일에 맞는 검증된 도구(MCP 서버)를 제안해
승인 한 번으로 설치합니다.

**상태: 프리알파.** 돈다; 아직 패키징 전이다. 당분간 macOS 전용.

**한 줄로:** Mac에 상주하며 캘린더·리마인더·메일·셸·Claude Code 를 지켜보다가, 말할 가치가 있을 때
먼저 말을 걸고, 승인 없이는 아무것도 하지 않는 비서. 하는 일 전부가
내 소유의 장부 한 줄이다.

```bash
brew install python@3.14 pipx ollama && pipx ensurepath && brew services start ollama && ollama signin
git clone <이 저장소> pathfinder && pipx install ./pathfinder
pathfinder init && pathfinder tui
```

아래 절들이 각 줄이 하는 일과 기대할 것을 설명한다.

## 첫 대화까지 10분

준비물(Homebrew). macOS에 딸려 오는 Python은 너무 오래됐다; PATHFINDER는 3.11 이상이 필요하다.

```bash
brew install python@3.14 pipx ollama node   # node는 도구 설치(npx)에만 필요; 지금은 건너뛰어도 된다
pipx ensurepath                          # 한 번만; 그다음 새 터미널을 연다
brew services start ollama               # 또는: ollama serve
ollama signin                            # 브라우저로 ollama.com이 열린다; 무료 플랜으로 충분하다
```

설치와 설정:

```bash
git clone <이 저장소> pathfinder && cd pathfinder
pipx install .                           # `pathfinder` 를 PATH 에 올린다; 셸과 Claude Code 훅이 이 이름으로 부른다
pathfinder init
```

(Homebrew 의 Python 은 자기 안으로의 맨 `pip install` 을 거부한다, PEP 668; `pipx` 가 PATHFINDER 에
자기 환경과 PATH 위의 명령을 준다.)

`init`은 Python과 Ollama를 검사하고, 내 Ollama가 닿는 모델 목록을 보여 주어 하나를 고르게 하고,
어떤 감각을 켤지 묻고, 셸과 Claude Code에 훅을 걸지 제안하고, launchd로 로그인 시 데몬을
시작하게 한다. 그다음:

```bash
pathfinder tui                           # 화면 하나: 입력, 읽기, /approve, 알림에 +/-, /quit
pathfinder say "안녕"                    # 또는 한 번에 한 마디
```

어느 단계가 실패하면 `init`은 정확한 해결 줄을 출력하고 멈춘다; 고친 뒤 다시 돌리면 된다 - 이미
된 것은 건드리지 않는다.

## 무엇이 어디서 도나

| 자리 | 무엇 |
|---|---|
| `~/.pathfinder/` (`$PATHFINDER_HOME`) | PATHFINDER 소유 전부: 장부(`events/`), `config.json`, `RULES.md`, `USER.md`, `wiki/`, 소켓, `daemon.log` |
| `~/Library/LaunchAgents/pathfinder.daemon.plist` | `init`이 쓴 launchd 에이전트; `pathfinder service start\|stop\|restart\|status` |
| `~/.zshrc`(가드된 한 줄)와 `~/.pathfinder/hooks/pathfinder.zsh` | 셸 감각, 예라고 했을 때만 |
| `~/.claude/settings.json`(훅 두 항목) | Claude Code 감각, 예라고 했을 때만 |
| `~/.claude.json`(`mcpServers`) | 승인한 도구; Claude Code도 같이 본다 |
| macOS 키체인 | 모든 비밀(MCP 서버 토큰, 메일 비밀번호), `/secret` 으로 넣는다; 파일에는 절대 없다 |

`ARCHITECTURE.ko.md`(와 `ARCHITECTURE.md`)가 모듈 하나하나를 설명한다;
`docs/decisions/DECISIONS.md`는 결정 목록이며 각각을 뒤집을 조건이 적혀 있다.

## 감각 더 켜기

`init`은 비밀이 필요 없는 감각을 켜고, 필요한 둘에 대해서는 나중에 붙여 넣을 정확한 줄을 찍어 준다.
한 줄에 감각 하나씩이고, 다른 감각은 켜진 채로 남는다:

```bash
pathfinder config set senses.calendar '{}'
pathfinder config set senses.mail '{"host": "imap.example.com", "user": "you@example.com"}'
pathfinder secret mail                   # IMAP 비밀번호, 가려 쳐서 키체인에 (화면에서는 /secret mail)
```

메일이 켜져 있는데 비밀번호가 없으면 화면이 붙을 때 묻는다.
캘린더 감각이 처음 돌 때 macOS가 캘린더 접근을 묻는다.

## 도구 설치

```bash
pathfinder search "postgres"             # MCP 레지스트리의 로컬 사본, 카탈로그 감각이 채운다
pathfinder install <레지스트리 이름>      # 제안; 행에 검사 결과와 발견한 툴이 보인다
pathfinder approve ID                    # config.json과 ~/.claude.json에 설치; pathfinder uninstall <id>가 되돌린다
```

또는 데몬이 지금 연 프로젝트에 맞는 도구를 제안하면 "설치해"라고 말하면 된다.

토큰이 필요한 서버는 시험 전에 멈추고, 화면이 토큰을 가려서 묻는다. 직접 넣으려면 화면에서
`/secret <id> NAME`, 셸에서 `pathfinder secret <id> NAME` 을 친 뒤 설치를 다시 청한다. 값은 절대
채팅을 지나지 않는다. 실수로 채팅에 붙여 넣은 토큰은 키체인에 `pasted-<n>` 으로 들어가고, 화면이
묶는 법(`/secret <id> NAME --from pasted-<n>`)을 알려 준다.

## 업그레이드

이 버전으로 올린 뒤에는 데몬을 멈춘 채 파생 상태를 한 번 다시 지어서, 지난 턴도 툴 호출
기록(`messages.ref` 칸)을 갖게 한다:

```bash
pathfinder service stop
pathfinder reindex
pathfinder service start
```

`pathfinder service restart`(또는 stop 과 start)는 화면의 연결을 끊는다: `pathfinder tui` 를
다시 띄워 붙는다.

## 모델 제공자

OpenAI 호환 엔드포인트면 무엇이든: `pathfinder config set model provider:model-name`, 제공자의
`base_url`과 `api_key_env`는 `config.json`에. Anthropic 네이티브 어댑터도 있지만
(`providers.<name>.api = "anthropic"`) **실험적**이다: 녹화된 응답으로만 테스트했고 라이브로
돌린 적이 없다.

## 실측

모든 구획은 실측을 거쳐 들어왔다(`HISTORY.md`); `bench/` 아래 벤치가 그것을 다시 돌린다.
`python3 -m unittest discover -s tests`가 오프라인 스위트를 돈다, 네트워크 없이.

## 기여 위생

`git config core.hooksPath .githooks`가 커밋 전 개인정보 스캔(`scripts/scan_personal.py`)을 켠다.
자리표시자만: `admin@example.com`, `sk-...`. 추적되는 파일에 모델 이름은 없다: `config.json`에 산다.

라이선스: Apache-2.0 (`LICENSE` 참조).

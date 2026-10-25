
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>AI 에이전트를 위한 태스크 운영체제 — 에이전트가 자기 작업을 증명하게 만듭니다.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | 한국어 | <a href="README.es.md">Español</a> | <a href="README.pt-BR.md">Português (Brasil)</a> | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.tr.md">Türkçe</a> | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="mini-ork IDE에서 실행한 code-fix: 분류, 계획, 구현, 검증, 세 가지 리뷰 렌즈, 게시를 거친 뒤 변경된 파일마다 Keep 또는 Reject를 고릅니다" width="900">
  </a>
</p>
<p align="center"><sub>mini-ork IDE에서 실행한 <code>code-fix</code>: 분류 → 계획 → 구현 → 검증 → 세 모델 계열에 걸친 세 가지 리뷰 렌즈 → 게시. 이후 변경된 파일마다 Keep 또는 Reject 결정을 기다립니다. <a href="https://miniork.dev/">워크스루</a>의 샘플 실행 · <a href="https://miniork.dev/media/mini-ork-demo.mp4">원본 화질 영상</a>.</sub></p>

mini-ork는 목표를 여러 모델로 이루어진 플릿 위에서 계획하고 실행하고 **검증**하는
실행(run)으로 바꿉니다: **분류 → 계획 → 실행 → 검증 → 회고 → 개선**. 모든 판정은
모델이 자기 출력을 두고 내린 의견이 아니라, 코드를 실제로 실행했을 때의 결과에서
나옵니다. 테스트, 타입 체크, 스키마, 격리된 샌드박스에서의 실행이 그 근거입니다.

이 페이지는 설치 가이드입니다. 프로젝트의 배경이 되는 논지는
[mini-ork가 존재하는 이유](docs/WHY.md)를 참고하세요.

## 빠른 시작

macOS, Linux 또는 WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

설치 프로그램이 하는 일:

- mini-ork를 `~/.local/share/mini-ork`에 클론합니다(`MINI_ORK_INSTALL_DIR`로 바꿀 수 있습니다).
- 체크아웃 로컬 `.venv`를 만들고 전체 Python 프로필
  (CLI, 로컬 웹 사이드카, Crucible)을 설치합니다.
- `~/.local/bin`에 `mini-ork`를 두어 PATH에 올립니다.
- 터미널에서 실행 중이면 첫 실행 마법사를 띄울지 묻습니다.

기존 체크아웃에서 같은 방식으로 설치하려면:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules`는 IDE 크레이트인 `ui/`
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui))를 채웁니다. Python
런타임에는 필요하지 않습니다. IDE를 빌드하려면 [docs/UI.md](docs/UI.md)를 참고하세요.

## 첫 실행: `mini-ork onboard`

명령 하나로 새 머신을 레시피를 실행할 수 있는 워크스페이스로 만듭니다. 모델을
호출하지 않고 비용도 쓰지 않습니다. 모든 단계는 로컬 점검이거나 로컬 빌드입니다.

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| 단계 | 수행 내용 |
|---|---|
| 사전 요구 사항 | Python ≥ 3.11과 git을 확인하고, `sqlite3` / `jq`가 있으면 기록합니다 |
| 워크스페이스 | 현재 디렉터리에 `.mini-ork/`를 만듭니다(필요하면 `git init` 실행) |
| 프로바이더 레인 | 자격 증명이 저장되어 있는지 보고하거나, 이를 추가하는 명령을 알려 줍니다 |
| contextnest | *선택 사항:* 로컬 메모리 서비스를 클론하고 빌드한 뒤 초기 설정을 만듭니다 |
| zed | *선택 사항:* mini-ork의 에이전트와 MCP 서버를 Zed의 `settings.json`에 연결합니다 |

선택 구성 요소는 해당 툴체인(`cargo`, `zed`)이 있을 때만 제안되며, 각각을
명시적으로 고를 수도 있습니다:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

종료 코드: `0` 완료(건너뛴 구성 요소가 있어도 괜찮습니다), `1` `--strict`에서 선택한
구성 요소가 실패함, `2` 사용법 오류. 비대화형 실행에서는 프롬프트가 뜨지 않으며,
`-y`는 모든 기본값을 수락합니다.

### ContextNest — 로컬 메모리 기반

과거 세션이 무엇을 결정했고, 무엇에 실패했고, 무엇을 검증하고 출시했는지 기억하는
Rust HTTP 서비스입니다. 마법사는 **simple** 티어로 `config/contextnest.yaml`을
생성합니다. `--secure`를 지정하면 아무 파일도 쓰지 않으며, 서비스를 시작하려면 `CN_TOKEN`이 필요합니다.

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

직접 설치하려면 `git clone https://github.com/SourceShift/ContextNest.git` 후
`make cn-serve`를 실행합니다(`127.0.0.1:28080`에 바인딩). Rust 1.80 이상이 필요합니다. 자세한 내용:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md).

### Zed — 에디터 통합

`mini-ork acp`(외부 에이전트)와 `mini-ork mcp-context`(읽기 전용 실행·학습·비용
컨텍스트)를 Zed에 연결합니다. `settings.json`에 쓰기 전에는 항상 타임스탬프가 붙은
백업을 먼저 만듭니다.

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

[docs/ZED.md](docs/ZED.md)와 [docs/UI.md](docs/UI.md)를 참고하세요.

## 첫 번째 검증 워크플로

~~~bash
cd ~/my-project
mini-ork onboard                      # initialize the project
mini-ork providers configure <lane>   # hidden prompt; keys never touch argv
mini-ork doctor                       # dependencies, environment, provider preflight

cp ~/.local/share/mini-ork/examples/01-hello-world/kickoff.md ./kickoff.md

# Check the wiring first: no model calls, no spend.
MINI_ORK_DRY_RUN=1 mini-ork run code-fix ./kickoff.md
mini-ork validate

# Then run it for real.
mini-ork run code-fix ./kickoff.md
~~~

킥오프에는 대상 리포지토리, 수정을 허용할 파일, 의도한 산출물, 그리고 성공을 증명하는
명령이나 규칙을 적습니다. 실행 산출물은 `.mini-ork/runs/`에, 기록된 상태는
`.mini-ork/state.db`에 저장됩니다. 어떤 레인이 어떤 역할을 맡을지 바꾸려면
`.mini-ork/config/agents.yaml`을 확인하세요.

### 명령 하나로 수정 증명하기

`mini-ork certify`는 변경 사항과 그 변경이 고쳤다고 주장하는 버그를 받아, 여러분의
코드에서 버그를 재현하고, 변경이 염두에 두지 않은 입력을 생성하고, 모든 것을 Docker
샌드박스에서 실행한 뒤, 증거를 첨부한 판정을 돌려줍니다:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

종료 코드는 `0` PROVEN, `1` REFUTED, `2` UNVERIFIED입니다. 추측하는 대신 판정을
보류하므로 CI 게이트로 쓸 수 있습니다. Docker와 모델 레인 하나가 필요합니다.
`bash examples/certify-demo/demo.sh`로 직접 해 보세요.

## 검증, 학습, 자기 개선

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="miniork.dev의 영역 맵: 실행 루프, 학습 루프, 수리 루프, RSI 루프가 차례로 켜지며, 각 루프에서 엔진·검증·학습·복구 사이에 무엇이 흐르는지 보여 줍니다" width="900">
  </a>
</p>
<p align="center"><sub>하나의 엔진을 둘러싼 네 개의 루프. <a href="https://miniork.dev/">워크스루</a>에서 발췌(샘플 데이터) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">원본 화질 영상</a>.</sub></p>

이 페이지 상단의 GIF가 실행 루프입니다. 그 둘레에 세 개의 층이 더 있습니다.

### 에이전트가 말로 빠져나갈 수 없는 검증

모든 판정은 실행 증거입니다. 레시피의 검증기는 변조 방지 가드, 테스트 스위트, 베이스
재실행(테스트가 이전 코드에서는 실패해야 함), 뮤테이션 적정성 검사를 실행합니다.
구현자가 작업을 시작하기 전에 두 번째 모델 레인이 킥오프만 보고 블라인드 스펙 테스트를
작성하며, 구현자는 이 테스트를 수정할 수 없습니다. 퍼블리셔의 게이트 체인은 통과하지
못한 것을 모두 거부하고, 리뷰어 모델은 결과를 거부할 수는 있어도 통과를 만들어 낼 수는 없습니다.
`certify`는 같은 원칙을 단일 변경에 적용해 PROVEN 또는 REFUTED로 답하고, 어느 쪽도
증명할 수 없으면 UNVERIFIED로 답합니다. 적대적 SWE-bench 코퍼스에서 PROVEN 판정
21건 중 21건이 정확했고 거짓 완료는 0건이었습니다. 방법론과 한계는
[docs/RESULTS.md](docs/RESULTS.md)에, 메커니즘은
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md)에 정리되어 있습니다.

### 검증된 것만 남기는 학습

매 실행이 끝나면 트레이스가 노드별 프로세스 보상으로 바뀌며, 양의 보상에는 반드시
검증기 증거가 짝지어져야 합니다. 여기서 루프는 여러 갈래로 나뉩니다. 라우터가 학습하는
레인별 어드밴티지(GRPO), 회고 단계가 교훈으로 추출하는 텍스트 그래디언트, 그리고 교훈을
기록하기 전에 거치는 충돌 검사입니다. 검증된 교훈만 프롬프트에 들어가고, 모든 프롬프트에는
학습된 블록이 포함되며, 주입 원장이 각 노드가 받은 내용을 정확히 기록하므로 공로는 그
성과를 낸 교훈에 돌아갑니다. 현재 그래디언트와 검증된 테마는 실제로 프롬프트에 반영되지만,
학습된 라우팅이 결정하는 경로는 아직 소수에 그칩니다. 수명 주기와 현황:
[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md).

### 게이트로 통제되는 자기 개선

mini-ork는 같은 레시피 그래프로 mini-ork 자신을 수정합니다. `framework-edit`(블라인드
스펙 테스트로 검증되는 자기 수정), `self-improve`(스캔, 렌즈, 종합, 구현, 게이트),
`rsi-technique-review`(문헌을 읽고 구현할 기법을 제안), 그리고 `goal-loop`입니다.
학습된 변경은 해킹 가능성 감사와 검증기 감사를 통과한 뒤, 블라인드 재실행 대조군과
비교하는 고정 프로브 기반 비회귀 게이트까지 통과해야 하며, 승격된 모든 버전에는 롤백
포인터가 남습니다. 사람 없이 프롬프트를 다시 쓰는 적용 루프는 **기본적으로 꺼진 상태**로
배포됩니다(아래 경고 참고). `framework-edit`가 가장 많이 실행되는 레시피이며,
`self-improve`는 2026-10-23에 자신에 대한 수정 사항을 스스로 찾아 병합했습니다. 자세한 내용:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

## 자주 쓰는 명령

| 명령 | 기능 |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | 레시피 하나를 처음부터 끝까지 실행 |
| `mini-ork doctor` | 의존성, 환경, 프로바이더 사전 점검 확인 |
| `mini-ork validate` | `Fix:` 힌트가 포함된 정적 사전 실행 검사 |
| `mini-ork providers status <lane>` | 자격 증명의 존재 여부와 출처 표시(값은 절대 표시하지 않음) |
| `mini-ork serve` | `127.0.0.1:7090`에서 로컬 웹 UI와 API 제공 |
| `mini-ork garden` | 드리프트 감지: 충돌, 고아 항목, 오래된 실행 |
| `mini-ork recover` / `resume` | 실패했거나 중단된 실행 되살리기 |
| `mini-ork mcp` / `vault` | MCP 서버 레지스트리, 시크릿 볼트 백엔드 체인 |
| `mini-ork zed setup` / `cn status` | 에디터 연결, ContextNest 티어와 도달 가능성 |

시작용 레시피: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`. [recipes/](recipes/)와
[확장 가이드](docs/EXTENSION.md)를 참고하세요.

## 업그레이드

설치 프로그램을 다시 실행하거나, 체크아웃에서 `make install`을 실행하세요. 여러 번
실행해도 안전합니다. `.venv`를 재사용하고, editable 패키지를 업데이트하고, 관리 대상 명령을
복구하고, OS 도구를 확인합니다. 그다음 `mini-ork update`가 대기 중인 데이터베이스
마이그레이션을 적용하고 설정 드리프트를 보고합니다.

## 제거

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

프로젝트 상태는 각 프로젝트의 `.mini-ork/`에 있습니다. 지우고 싶은 곳에서 삭제하세요.
소스 체크아웃과 git 기록은 절대 건드리지 않습니다.

## 요구 사항

| | |
|---|---|
| OS | macOS, Linux, WSL. 네이티브 Windows: `winget`으로 Python 3.11, Git, jq, yq, SQLite를 설치한 뒤 `py -3 .\scripts\full_install.py`를 실행합니다 |
| Python | 3.11 또는 3.12 |
| Git | 필수 |
| Docker | 샌드박스 검증(`certify`, Crucible)에만 필요 |
| Rust 1.80+ | ContextNest와 IDE 크레이트에만 필요 |
| 모델 접근 | 실제 실행에는 설정된 프로바이더 레인 하나가 필요하며, 드라이런에는 필요 없음 |

## 더 알아보기

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | 프로젝트가 존재하는 이유, 측정 결과, 주장하지 않는 것 |
| [docs/SAFETY.md](docs/SAFETY.md) | 자기 수정에 대한 원칙과 이를 지키는 게이트 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 시스템 설계와 컴포넌트 다이어그램 |
| [docs/operator](docs/operator) | mini-ork 운영: 환경 변수, 문제 해결 |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | mini-ork를 여러분의 앱에 임베드하기 |
| [docs/RESULTS.md](docs/RESULTS.md) | 수치 뒤에 있는 방법론과 표본 크기 |
| [ROADMAP.md](ROADMAP.md) | 프로젝트가 나아갈 방향 |

> [!WARNING]
> mini-ork에는 자신의 프롬프트와 워크플로를 다시 쓰고, 사람의 검토 없이 그 변경을
> 승격할 수 있는 적용 루프가 들어 있습니다. 이 루프는 **기본적으로 꺼져 있으며**
> (`MO_APPLY_ENABLED`와 `MO_AUTO_APPLY`가 모두 `1`이어야 함), 영향 범위, 상한,
> 롤백은 [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended)에 문서화되어 있습니다.

## 기여 및 라이선스

mini-ork는 **Apache-2.0** 라이선스이며, © Amir Khakshour입니다. 아직 초기 단계의 연구용
프로젝트입니다. 프레임워크 변경은 전용 워크트리에서 하고, 모든 동작 주장에는 검증기를
함께 두고, 변경하는 영역에 맞는 집중 검사를 실행하세요. 기여 워크플로는
[AGENTS.md](AGENTS.md)를, 프로젝트 방향은 [GOVERNANCE.md](GOVERNANCE.md)를 참고하세요.

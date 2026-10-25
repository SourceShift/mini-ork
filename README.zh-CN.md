
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>面向 AI 智能体的任务操作系统——让它们用证据证明自己的工作。</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | 简体中文 | <a href="README.ja.md">日本語</a> | <a href="README.ko.md">한국어</a> | <a href="README.es.md">Español</a> | <a href="README.pt-BR.md">Português (Brasil)</a> | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.tr.md">Türkçe</a> | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="在 mini-ork IDE 中运行一次 code-fix：分类、规划、实现、验证、三个评审视角、发布，最后逐个文件选择 Keep 或 Reject" width="900">
  </a>
</p>
<p align="center"><sub>在 mini-ork IDE 中运行一次 <code>code-fix</code>：分类 → 规划 → 实现 → 验证 → 由三个模型家族分别承担三个评审视角 → 发布，随后每个改动的文件等待 Keep 或 Reject。示例运行取自<a href="https://miniork.dev/">产品导览</a> · <a href="https://miniork.dev/media/mini-ork-demo.mp4">高清视频</a>。</sub></p>

mini-ork 把一个目标转化为一次跨多个模型完成规划、执行并经过**验证**的运行：**分类 → 规划 → 执行 → 验证 → 反思 → 改进**。每个判定都来自代码运行时的真实表现——测试、类型检查、schema 校验、隔离沙箱中的执行——而不是模型对自身输出的评价。

本页是安装指南。想了解项目背后的思考，请阅读[为什么要做 mini-ork](docs/WHY.md)。

## 快速开始

macOS、Linux 或 WSL：

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

安装程序会：

- 把 mini-ork 克隆到 `~/.local/share/mini-ork`（可用 `MINI_ORK_INSTALL_DIR` 覆盖）；
- 在检出目录内创建 `.venv`，并安装完整的 Python 组件（CLI、本地 Web sidecar、Crucible）；
- 把 `mini-ork` 放进 `~/.local/bin`，加入你的 PATH；
- 在终端中运行时，提供首次运行向导。

如果已经有检出副本，用下面的方式完成同样的安装：

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` 会拉取 `ui/`，即 IDE crate（[`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)）。Python 运行时并不依赖它；如需构建 IDE，请参阅 [docs/UI.md](docs/UI.md)。

## 首次运行：`mini-ork onboard`

一条命令就能把一台全新的机器配置成可以运行 recipe 的工作区。它从不调用模型，也不花一分钱——每一步都是本地检查或本地构建。

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| 步骤 | 做了什么 |
|---|---|
| 前置条件 | 检查 Python ≥ 3.11 和 git；如已安装 `sqlite3` / `jq`，一并记录 |
| 工作区 | 在当前目录创建 `.mini-ork/`（必要时执行 `git init`） |
| 提供商通道 | 报告凭据是否已保存；未保存时给出添加凭据的命令 |
| contextnest | *可选*：克隆并构建本地记忆服务，并生成初始配置 |
| zed | *可选*：把 mini-ork 的智能体和 MCP 服务器接入 Zed 的 `settings.json` |

可选组件只在对应工具链（`cargo`、`zed`）存在时才会提示，也可以逐一显式指定：

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

退出码：`0` 完成（跳过的组件不算失败）；`1` 在 `--strict` 下某个选中的组件失败；`2` 用法错误。非交互运行不会弹出任何提示：`-y` 接受全部默认值。

### ContextNest——本地记忆底座

一个用 Rust 编写的 HTTP 服务，记住以往会话做过的决策、踩过的坑、验证过的结论和交付过的成果。向导默认按**简单**档位生成 `config/contextnest.yaml`；使用 `--secure` 时不写入任何文件，并要求先提供 `CN_TOKEN`，服务才会启动。

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

手动安装：先 `git clone https://github.com/SourceShift/ContextNest.git`，再执行 `make cn-serve`（监听 `127.0.0.1:28080`）。需要 Rust 1.80+。详见 [docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md)。

### Zed——编辑器集成

把 `mini-ork acp`（外部智能体）和 `mini-ork mcp-context`（只读的运行、学习与成本上下文）接入 Zed。每次写入 `settings.json` 之前，都会先生成一份带时间戳的备份。

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

参见 [docs/ZED.md](docs/ZED.md) 和 [docs/UI.md](docs/UI.md)。

## 你的第一个经过验证的工作流

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

kickoff 文件写明目标仓库、允许修改的文件、预期产物，以及判定成功的命令或规则。运行产物存放在 `.mini-ork/runs/`，状态记录在 `.mini-ork/state.db`。如需调整各角色由哪个通道承担，请查看 `.mini-ork/config/agents.yaml`。

### 一条命令证明修复有效

`mini-ork certify` 接收一处改动及其声称修复的 bug：先在你的代码上复现这个 bug，再生成这处改动没有针对过的输入，把一切放进 Docker 沙箱运行，最后返回附带证据的判定：

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

退出码：`0` PROVEN，`1` REFUTED，`2` UNVERIFIED。拿不准时它宁可弃权也不瞎猜，因此可以直接用作 CI 门禁。需要 Docker 和一个模型通道。不妨试试 `bash examples/certify-demo/demo.sh`。

## 验证、学习与自我改进

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="miniork.dev 上的分区图：运行循环、学习循环、修复循环和 RSI 循环依次点亮，分别展示引擎、验证、学习与恢复之间流转的内容" width="900">
  </a>
</p>
<p align="center"><sub>围绕同一个引擎的四个循环，取自<a href="https://miniork.dev/">产品导览</a>（示例数据）· <a href="https://miniork.dev/media/mini-ork-loops.mp4">高清视频</a>。</sub></p>

本页顶部的 GIF 展示的就是运行循环。在它外面还有三层。

### 智能体无法靠话术蒙混过关的验证

每个判定都以执行证据为准。recipe 的验证器会运行防篡改检查、测试套件、基线回放（测试必须在旧代码上失败）以及变异充分性检查。在实现者动手之前，另一个模型通道会根据 kickoff 盲写规格测试，实现者无权修改。发布器的门禁链拒绝一切未通过的结果；评审模型可以否决结果，却永远无法凭空制造一次通过。`certify` 对单个改动采取同样的立场，给出 PROVEN 或 REFUTED，两者都无法证明时给出 UNVERIFIED。在一个对抗性 SWE-bench 语料上，21 个 PROVEN 判定中 21 个正确，虚假完成为 0；方法与局限见 [docs/RESULTS.md](docs/RESULTS.md)，机制说明见 [docs/architecture/verification-stack.md](docs/architecture/verification-stack.md)。

### 只留下经过验证的学习成果

每次运行结束后，执行轨迹会转化为逐节点的过程奖励，而正向奖励必须有验证器证据作支撑。循环由此分出几路：路由器据以学习的各通道优势值（GRPO）；反思步骤从中提炼经验的文本梯度；以及写入任何经验之前的冲突检查。只有经过验证的经验才会进入提示词，每个提示词都带有学习所得的内容块，注入台账精确记录每个节点收到了什么，功劳因此能回溯到真正起作用的那条经验。目前，梯度和已验证的主题确实已经流入提示词，而学习得出的路由仍只决定一小部分路由。生命周期与现状：[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md)。

### 有门禁把守的自我改进

mini-ork 通过同一张 recipe 图修改 mini-ork 自身：`framework-edit`（配有盲写规格测试、经过验证的自我修改）、`self-improve`（扫描、多视角评审、综合、实现、门禁）、`rsi-technique-review`（阅读文献，提出值得实现的技术）以及 `goal-loop`。学习得到的改动必须先通过防钻空子审计和验证器审计，再在冻结探针上、以盲重跑对照组为基准通过不回退门禁；每个晋升的版本都保留回滚指针。无人值守改写提示词的 apply 循环**默认关闭**（见下方警告）。`framework-edit` 是运行次数最多的 recipe；2026-10-23，`self-improve` 自行发现问题并合入了自己的修复。详见 [docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md)、[docs/SAFETY.md](docs/SAFETY.md)、[docs/ECOSYSTEM.md](docs/ECOSYSTEM.md)。

## 常用命令

| 命令 | 作用 |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | 端到端运行一个 recipe |
| `mini-ork doctor` | 检查依赖、环境与提供商预检 |
| `mini-ork validate` | 运行前静态检查，附 `Fix:` 修复提示 |
| `mini-ork providers status <lane>` | 显示凭据是否存在及其来源（绝不显示值） |
| `mini-ork serve` | 本地 Web UI 与 API，地址 `127.0.0.1:7090` |
| `mini-ork garden` | 漂移检测：冲突、孤儿项、陈旧运行 |
| `mini-ork recover` / `resume` | 恢复失败或中断的运行 |
| `mini-ork mcp` / `vault` | MCP 服务器注册表；密钥保险库后端链 |
| `mini-ork zed setup` / `cn status` | 编辑器接入；ContextNest 档位与可达性 |

入门 recipe：`code-fix`、`docs`、`refactor-audit`、`bug-audit-cmgk`、`research-synthesis`、`spec-driven-delivery`。参见 [recipes/](recipes/) 和[扩展指南](docs/EXTENSION.md)。

## 升级

重新运行安装程序，或在检出目录中执行 `make install`。重复执行是安全的：它会复用 `.venv`、更新可编辑安装的包、修复受管命令，并校验操作系统工具。之后运行 `mini-ork update`，应用待执行的数据库迁移并报告配置漂移。

## 卸载

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

项目状态保存在各项目的 `.mini-ork/` 中，想清掉哪个就删哪个。你的源码检出和 git 历史永远不会被改动。

## 环境要求

| | |
|---|---|
| 操作系统 | macOS、Linux、WSL。原生 Windows：先用 `winget` 安装 Python 3.11、Git、jq、yq 和 SQLite，再运行 `py -3 .\scripts\full_install.py` |
| Python | 3.11 或 3.12 |
| Git | 必需 |
| Docker | 仅沙箱验证需要（`certify`、Crucible） |
| Rust 1.80+ | 仅 ContextNest 和 IDE crate 需要 |
| 模型访问 | 真实运行需配置一个提供商通道；试运行无需任何配置 |

## 了解更多

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | 项目为何存在；实测数据；以及它不做哪些承诺 |
| [docs/SAFETY.md](docs/SAFETY.md) | 自我修改的安全立场，以及守住底线的门禁 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统设计与组件图 |
| [docs/operator](docs/operator) | 运行 mini-ork：环境变量、故障排查 |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | 把 mini-ork 嵌入你自己的应用 |
| [docs/RESULTS.md](docs/RESULTS.md) | 各项数据背后的方法与样本量 |
| [ROADMAP.md](ROADMAP.md) | 项目的发展方向 |

> [!WARNING]
> mini-ork 包含一个 apply 循环，它可以改写自身的提示词和工作流，并在无人审核的情况下晋升这些改动。该循环**默认关闭**（必须同时将 `MO_APPLY_ENABLED` 和 `MO_AUTO_APPLY` 设为 `1`），其影响范围、上限与回滚机制见 [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended)。

## 贡献与许可证

mini-ork 采用 **Apache-2.0** 许可证，© Amir Khakshour。项目尚处早期，属于研究级质量：修改框架请使用专门的 worktree，每一项行为声明都要配一个验证器，并针对你改动的部分运行相应的检查。贡献流程见 [AGENTS.md](AGENTS.md)，项目方向见 [GOVERNANCE.md](GOVERNANCE.md)。

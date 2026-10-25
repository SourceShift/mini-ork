
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>AI agents के लिए एक task operating system — जो उनसे अपना काम साबित करवाता है।</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | <a href="README.ko.md">한국어</a> | <a href="README.es.md">Español</a> | <a href="README.pt-BR.md">Português (Brasil)</a> | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | हिन्दी | <a href="README.it.md">Italiano</a> | <a href="README.tr.md">Türkçe</a> | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="mini-ork IDE में एक code-fix run: classify, plan, implement, verify, तीन review lenses, publish, फिर हर बदली गई फ़ाइल के लिए Keep या Reject" width="900">
  </a>
</p>
<p align="center"><sub>mini-ork IDE में एक <code>code-fix</code> run: classify → plan → implement → verify → तीन model families पर तीन review lenses → publish, फिर हर बदली गई फ़ाइल Keep या Reject का इंतज़ार करती है। <a href="https://miniork.dev/">वॉकथ्रू</a> से लिया गया एक sample run · <a href="https://miniork.dev/media/mini-ork-demo.mp4">पूरी क्वालिटी का वीडियो</a>।</sub></p>

mini-ork किसी लक्ष्य को models के एक fleet पर चलने वाले योजनाबद्ध, निष्पादित और **सत्यापित**
run में बदल देता है: **classify → plan → execute → verify → reflect → improve**। हर verdict
इस पर आधारित होता है कि चलाए जाने पर code ने असल में क्या किया — tests, type checks, schemas,
एक isolated sandbox में execution — न कि अपने ही output पर किसी model की राय पर।

यह पेज install guide है। प्रोजेक्ट के पीछे का तर्क जानने के लिए
[mini-ork क्यों मौजूद है](docs/WHY.md) देखें।

## त्वरित शुरुआत

macOS, Linux या WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

Installer ये काम करता है:

- mini-ork को `~/.local/share/mini-ork` में clone करता है (`MINI_ORK_INSTALL_DIR` से इसे बदला जा सकता है),
- एक checkout-local `.venv` बनाता है और पूरा Python profile install करता है
  (CLI, local web sidecar, Crucible),
- `~/.local/bin` में `mini-ork` को आपके PATH पर रखता है,
- terminal पर चलने पर first-run wizard चलाने का विकल्प देता है।

किसी मौजूदा checkout से, यही install:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` से `ui/` भर जाता है, जो IDE crate है
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui))। Python
runtime को इसकी ज़रूरत नहीं है; IDE build करने के लिए [docs/UI.md](docs/UI.md) देखें।

## पहला run: `mini-ork onboard`

एक command किसी नई machine को ऐसे workspace तक पहुँचा देता है जिसमें आप recipe चला सकें। यह कभी
किसी model को call नहीं करता और कभी पैसा खर्च नहीं करता — हर step एक local check या local build है।

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| चरण | क्या होता है |
|---|---|
| prerequisites | Python ≥ 3.11 और git की जाँच करता है; `sqlite3` / `jq` मौजूद हों तो उन्हें नोट करता है |
| workspace | मौजूदा directory में `.mini-ork/` बनाता है (ज़रूरत हो तो `git init` चलाता है) |
| provider lanes | बताता है कि credentials stored हैं या नहीं, या वह command बताता है जो उन्हें जोड़ता है |
| contextnest | *वैकल्पिक:* local memory service को clone और build करता है, और उसके लिए शुरुआती config लिखता है |
| zed | *वैकल्पिक:* mini-ork के agent + MCP servers को Zed की `settings.json` से जोड़ता है |

वैकल्पिक components तभी पेश किए जाते हैं जब उनका toolchain मौजूद हो (`cargo`, `zed`),
और हर एक को स्पष्ट रूप से चुना जा सकता है:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

Exit codes: `0` पूरा हुआ (skip किए गए components से कोई दिक्कत नहीं), `1` `--strict` के तहत कोई
चुना गया component fail हुआ, `2` usage error। Non-interactive run में कोई prompt नहीं आता: `-y`
हर default स्वीकार कर लेता है।

### ContextNest — स्थानीय memory substrate

एक Rust HTTP service जो याद रखती है कि पिछले sessions ने क्या तय किया, किसमें fail हुए, क्या verify
किया और क्या ship किया। Wizard **simple** tier में `config/contextnest.yaml` तैयार करता है; `--secure`
के साथ यह कुछ नहीं लिखता, और service शुरू होने से पहले `CN_TOKEN` ज़रूरी होता है।

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

हाथ से: `git clone https://github.com/SourceShift/ContextNest.git`, फिर
`make cn-serve` (`127.0.0.1:28080` पर bind करता है)। Rust 1.80+ ज़रूरी है। विवरण:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md)।

### Zed — editor के साथ integration

`mini-ork acp` (external agent) और `mini-ork mcp-context` (read-only run, learning और cost
context) को Zed से जोड़ता है। `settings.json` में हर write से पहले एक timestamped backup
लिया जाता है।

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

[docs/ZED.md](docs/ZED.md) और [docs/UI.md](docs/UI.md) देखें।

## आपका पहला सत्यापित workflow

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

Kickoff में target repository, अनुमत files, अपेक्षित artifact, और सफलता साबित करने वाला command
या नियम लिखा होता है। Run के artifacts `.mini-ork/runs/` में जाते हैं, और दर्ज state
`.mini-ork/state.db` में। कौन-सा lane कौन-सी role निभाए, यह बदलने के लिए
`.mini-ork/config/agents.yaml` देखें।

### एक command में fix साबित करें

`mini-ork certify` एक change और वह bug लेता है जिसे ठीक करने का वह दावा करता है, आपके code पर
bug को reproduce करता है, ऐसे inputs generate करता है जिनके लिए change लिखा ही नहीं गया था, सब कुछ
एक Docker sandbox में चलाता है, और evidence के साथ एक verdict लौटाता है:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

Exit codes `0` PROVEN, `1` REFUTED, `2` UNVERIFIED — यह अनुमान लगाने के बजाय फ़ैसला देने से बचता है,
इसलिए CI इस पर gate कर सकता है। Docker और एक model lane चाहिए।
`bash examples/certify-demo/demo.sh` आज़माएँ।

## सत्यापन, सीखना और स्व-सुधार

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="miniork.dev पर zone map: run loop, learning loop, repair loop और RSI loop बारी-बारी से रोशन होते हैं, और हर एक दिखाता है कि engine, verification, learning और recovery के बीच क्या प्रवाहित होता है" width="900">
  </a>
</p>
<p align="center"><sub>एक engine के चारों ओर चार loops, <a href="https://miniork.dev/">वॉकथ्रू</a> से (sample data) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">पूरी क्वालिटी का वीडियो</a>।</sub></p>

इस पेज के ऊपर वाला GIF run loop है। इसके चारों ओर तीन और layers हैं।

### ऐसा verification जिसे agents बातों से पार नहीं कर सकते

हर verdict execution का evidence है। किसी recipe के verifiers एक tamper guard, test suite, एक base
replay (test को पुराने code पर fail होना चाहिए) और एक mutation-adequacy check चलाते हैं। Implementer
के शुरू करने से पहले दूसरा model lane kickoff से blind spec tests लिखता है, और implementer उन्हें edit
नहीं कर सकता। Publisher की gate chain हर उस चीज़ को अस्वीकार कर देती है जो pass नहीं हुई, और reviewer
model किसी result को veto तो कर सकता है, पर कभी pass पैदा नहीं कर सकता। `certify` यही रुख एक change
पर लागू करता है और PROVEN या REFUTED जवाब देता है, और जब किसी भी पक्ष को साबित न कर सके तो UNVERIFIED।
एक adversarial SWE-bench corpus पर 21 में से 21 PROVEN verdicts सही थे, 0 false completions के साथ;
तरीका और उसकी सीमाएँ [docs/RESULTS.md](docs/RESULTS.md) में हैं, और mechanisms
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md) में।

### सीखना, जिसमें सिर्फ़ verified चीज़ें टिकती हैं

हर run के बाद traces हर node के लिए एक process reward बन जाते हैं, और positive reward के साथ verifier
evidence होना ज़रूरी है। वहाँ से loop कई दिशाओं में फैलता है: per-lane advantages, जिनसे router सीखता
है (GRPO); textual gradients, जिन्हें reflection step खँगालकर lessons में बदलता है; और कोई भी lesson
लिखे जाने से पहले एक conflict check। सिर्फ़ verified lessons ही prompt तक पहुँचते हैं, हर prompt में
learned block होता है, और एक injection ledger ठीक-ठीक दर्ज करता है कि हर node को क्या मिला, ताकि credit
उसी lesson तक लौटे जिसने उसे कमाया। आज gradients और verified themes सच में prompts तक पहुँचते हैं, जबकि
learned routing अभी भी routes के एक छोटे हिस्से का ही फ़ैसला करता है। Lifecycle और status:
[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md)।

### स्व-सुधार, gates के साथ

mini-ork उसी recipe graph के ज़रिए mini-ork को edit करता है: `framework-edit` (blind spec tests के साथ
verified self-edits), `self-improve` (scan, lenses, synthesis, implement, gates), `rsi-technique-review`
(literature पढ़कर बनाने लायक techniques सुझाता है) और `goal-loop`। किसी learned change को एक
hackability audit और एक verifier audit पास करना होता है, फिर frozen probes पर, blind-rerun control arm
के मुकाबले, एक non-regression gate; और हर promoted version एक rollback pointer रखता है। बिना निगरानी के
prompts को दोबारा लिखने वाला apply loop **default रूप से बंद** आता है (नीचे दी गई चेतावनी देखें)।
`framework-edit` सबसे ज़्यादा चलाया जाने वाला recipe है, और `self-improve` ने 2026-10-23 को अपना ही fix
ढूँढकर merge किया। विवरण:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md)।

## रोज़मर्रा के commands

| Command | क्या करता है |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | एक recipe को शुरू से अंत तक चलाता है |
| `mini-ork doctor` | dependencies, environment और provider preflight जाँचता है |
| `mini-ork validate` | `Fix:` hints के साथ run से पहले के static checks |
| `mini-ork providers status <lane>` | credential की मौजूदगी और source दिखाता है (values कभी नहीं) |
| `mini-ork serve` | `127.0.0.1:7090` पर local web UI और API |
| `mini-ork garden` | Drift का पता लगाना: collisions, orphans, stale runs |
| `mini-ork recover` / `resume` | fail हुए या बीच में रुके run को फिर से चालू करता है |
| `mini-ork mcp` / `vault` | MCP server registry; secret-vault का backend chain |
| `mini-ork zed setup` / `cn status` | Editor wiring; ContextNest का tier और reachability |

शुरुआती recipes: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`। [recipes/](recipes/) और
[extension गाइड](docs/EXTENSION.md) देखें।

## अपग्रेड

Installer फिर से चलाएँ, या किसी checkout से `make install` चलाएँ — इसे दोबारा चलाना सुरक्षित है: यह
`.venv` को फिर से इस्तेमाल करता है, editable package को update करता है, managed command की मरम्मत
करता है, और OS tools को verify करता है। इसके बाद `mini-ork update` लंबित database migrations लागू
करता है और config drift की रिपोर्ट देता है।

## अनइंस्टॉल

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

Project state हर project के `.mini-ork/` में रहती है; जहाँ इसे हटाना हो, वहाँ delete कर दें।
आपके source checkouts और git history को कभी छुआ नहीं जाता।

## आवश्यकताएँ

| | |
|---|---|
| OS | macOS, Linux, WSL। Native Windows: `winget` से Python 3.11, Git, jq, yq और SQLite install करें, फिर `py -3 .\scripts\full_install.py` चलाएँ |
| Python | 3.11 या 3.12 |
| Git | ज़रूरी |
| Docker | सिर्फ़ sandboxed verification के लिए (`certify`, Crucible) |
| Rust 1.80+ | सिर्फ़ ContextNest और IDE crate के लिए |
| Model access | असली runs के लिए एक configured provider lane; dry runs के लिए कुछ नहीं चाहिए |

## और जानें

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | प्रोजेक्ट क्यों मौजूद है; measurements; यह किन बातों का दावा नहीं करता |
| [docs/SAFETY.md](docs/SAFETY.md) | Self-modification को लेकर रुख और वे gates जो इसे थामे रखते हैं |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design और component diagrams |
| [docs/operator](docs/operator) | mini-ork चलाना: env vars, troubleshooting |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | mini-ork को अपने app में embed करें |
| [docs/RESULTS.md](docs/RESULTS.md) | आँकड़ों के पीछे के तरीके और sample sizes |
| [ROADMAP.md](ROADMAP.md) | प्रोजेक्ट किस दिशा में जा रहा है |

> [!WARNING]
> mini-ork में एक apply loop है जो अपने ही prompts और workflows को दोबारा लिख सकता है और बिना
> human review के उस बदलाव को promote कर सकता है। यह **default रूप से बंद** है (`MO_APPLY_ENABLED`
> और `MO_AUTO_APPLY` दोनों का `1` होना ज़रूरी है), और इसका blast radius, caps और rollbacks
> [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended) में documented हैं।

## योगदान और लाइसेंस

mini-ork **Apache-2.0** के तहत है, © Amir Khakshour। यह शुरुआती दौर का और research-grade है: framework
changes के लिए एक अलग worktree इस्तेमाल करें, हर behaviour claim के साथ एक verifier रखें, और जिस surface
को आप बदलते हैं उसके लिए focused checks चलाएँ। Contribution workflow के लिए [AGENTS.md](AGENTS.md) और
project की दिशा के लिए [GOVERNANCE.md](GOVERNANCE.md) देखें।

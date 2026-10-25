
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>AI エージェントに自分の仕事を証明させる、タスクオペレーティングシステム。</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | 日本語 | <a href="README.ko.md">한국어</a> | <a href="README.es.md">Español</a> | <a href="README.pt-BR.md">Português (Brasil)</a> | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.tr.md">Türkçe</a> | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="mini-ork IDE での code-fix 実行: 分類、計画、実装、検証、3 つのレビューレンズ、公開を経て、変更された各ファイルごとに Keep か Reject を選ぶ" width="900">
  </a>
</p>
<p align="center"><sub>mini-ork IDE での <code>code-fix</code> 実行: 分類 → 計画 → 実装 → 検証 → 3 つのモデルファミリーによる 3 つのレビューレンズ → 公開。その後、変更された各ファイルが Keep か Reject の判断を待ちます。<a href="https://miniork.dev/">ウォークスルー</a>のサンプル実行です · <a href="https://miniork.dev/media/mini-ork-demo.mp4">高画質版の動画</a>。</sub></p>

mini-ork は、目標を複数のモデルにまたがる実行に変え、計画・実行・**検証**までを一貫して行います: **分類 → 計画 → 実行 → 検証 → 振り返り → 改善**。すべての判定は、コードを実際に動かした結果、つまりテスト、型チェック、スキーマ、隔離されたサンドボックスでの実行から導かれます。モデルが自分の出力をどう評価したかではありません。

このページはインストールガイドです。プロジェクトの背景にある考え方は [mini-ork が存在する理由](docs/WHY.md) を参照してください。

## クイックスタート

macOS、Linux、WSL の場合:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

インストーラーは次の処理を行います:

- mini-ork を `~/.local/share/mini-ork` にクローンします（`MINI_ORK_INSTALL_DIR` で変更できます）。
- チェックアウト内に `.venv` を作成し、Python のフルプロファイル（CLI、ローカル Web サイドカー、Crucible）をインストールします。
- `mini-ork` を `~/.local/bin` に置き、PATH から使えるようにします。
- ターミナル上で実行されている場合は、初回セットアップウィザードを提示します。

既存のチェックアウトから同じインストールを行う場合:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` は IDE クレートである `ui/`（[`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)）を取得します。Python ランタイムには不要です。IDE のビルド方法は [docs/UI.md](docs/UI.md) を参照してください。

## 初回実行: `mini-ork onboard`

コマンド 1 つで、まっさらなマシンをレシピを実行できるワークスペースまで仕上げます。モデルを呼び出すことも、費用が発生することも一切ありません。すべてのステップはローカルでのチェックかローカルでのビルドです。

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| ステップ | 内容 |
|---|---|
| 前提条件 | Python ≥ 3.11 と git を確認します。`sqlite3` / `jq` があれば記録します |
| ワークスペース | カレントディレクトリに `.mini-ork/` を作成します（必要なら `git init` を実行） |
| プロバイダーレーン | 認証情報が保存済みかを報告し、未保存なら追加用のコマンドを示します |
| contextnest | *任意:* ローカルのメモリサービスをクローン・ビルドし、初期設定を生成します |
| zed | *任意:* mini-ork のエージェントと MCP サーバーを Zed の `settings.json` に登録します |

任意コンポーネントは、対応するツールチェーン（`cargo`、`zed`）がある場合にのみ提示されます。それぞれ明示的に選ぶこともできます:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

終了コード: `0` は完了（スキップしたコンポーネントがあっても問題ありません）、`1` は `--strict` 指定時に選択したコンポーネントが失敗、`2` は使い方の誤りです。非対話の実行ではプロンプトを出しません。`-y` ですべてのデフォルトを受け入れます。

### ContextNest — ローカルメモリ基盤

過去のセッションが何を決め、何に失敗し、何を検証し、何をリリースしたかを記憶する Rust 製の HTTP サービスです。ウィザードは **simple** ティアで `config/contextnest.yaml` を生成します。`--secure` を指定すると何も書き込まず、サービスの起動前に `CN_TOKEN` を必須にします。

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

手動で導入する場合は、`git clone https://github.com/SourceShift/ContextNest.git` の後に `make cn-serve` を実行します（`127.0.0.1:28080` で待ち受けます）。Rust 1.80 以上が必要です。詳細: [docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md)。

### Zed — エディター連携

`mini-ork acp`（外部エージェント）と `mini-ork mcp-context`（実行・学習・コストに関する読み取り専用のコンテキスト）を Zed に組み込みます。`settings.json` に書き込む前には、必ずタイムスタンプ付きのバックアップを取ります。

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

[docs/ZED.md](docs/ZED.md) と [docs/UI.md](docs/UI.md) を参照してください。

## 初めての検証付きワークフロー

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

キックオフには、対象リポジトリ、変更を許可するファイル、目的の成果物、そして成功を証明するコマンドまたはルールを書きます。実行の成果物は `.mini-ork/runs/` に、記録された状態は `.mini-ork/state.db` に保存されます。どのレーンがどのロールを担当するかを変えるには、`.mini-ork/config/agents.yaml` を確認してください。

### 修正をコマンド 1 つで証明する

`mini-ork certify` は、変更とその変更が直すと主張するバグを受け取り、あなたのコードに対してバグを再現し、変更が想定していない入力を生成し、すべてを Docker サンドボックスで実行して、証拠付きの判定を返します:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

終了コードは `0` が PROVEN、`1` が REFUTED、`2` が UNVERIFIED です。当て推量はせずに判定を保留するので、CI のゲートとして使えます。Docker とモデルレーンが 1 つ必要です。`bash examples/certify-demo/demo.sh` で試せます。

## 検証、学習、自己改善

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="miniork.dev のゾーンマップ: 実行ループ、学習ループ、修復ループ、RSI ループが順に点灯し、それぞれエンジン、検証、学習、リカバリーの間を何が流れるかを示す" width="900">
  </a>
</p>
<p align="center"><sub>1 つのエンジンを囲む 4 つのループ。<a href="https://miniork.dev/">ウォークスルー</a>より（サンプルデータ） · <a href="https://miniork.dev/media/mini-ork-loops.mp4">高画質版の動画</a>。</sub></p>

このページ冒頭の GIF が実行ループです。その周りに、さらに 3 つの層があります。

### エージェントが口先でごまかせない検証

判定はすべて実行による証拠に基づきます。レシピの検証器は、改ざんガード、テストスイート、ベースリプレイ（テストが旧コードで失敗することを確認）、ミューテーション十分性チェックを実行します。実装担当が作業を始める前に、別のモデルレーンがキックオフからブラインドのスペックテストを書き、実装担当はそれを編集できません。パブリッシャーのゲートチェーンは合格しなかったものをすべて拒否します。レビュー担当のモデルは結果を差し戻せますが、合格を作り出すことは決してできません。`certify` は同じ姿勢を 1 つの変更に適用し、PROVEN か REFUTED で答えます。どちらとも証明できなければ UNVERIFIED です。敵対的な SWE-bench コーパスでは、PROVEN 判定 21 件中 21 件が正しく、誤った完了判定は 0 件でした。手法と限界は [docs/RESULTS.md](docs/RESULTS.md)、仕組みは [docs/architecture/verification-stack.md](docs/architecture/verification-stack.md) にまとめています。

### 検証済みのものだけを残す学習

実行のたびに、トレースはノードごとのプロセス報酬に変換されます。正の報酬には、必ず検証器の証拠が伴わなければなりません。そこからループは枝分かれします。ルーターが学習に使うレーンごとのアドバンテージ（GRPO）、振り返りステップが教訓として掘り起こすテキスト勾配、そして教訓を書き込む前の矛盾チェックです。プロンプトに届くのは検証済みの教訓だけで、すべてのプロンプトが学習ブロックを持ちます。各ノードが受け取った内容は注入台帳が正確に記録するので、功績はそれをもたらした教訓へさかのぼって帰属されます。現時点では勾配と検証済みテーマが実際にプロンプトへ流れ込んでいますが、学習済みルーティングが決めているのはルート全体のごく一部にとどまります。ライフサイクルと現状: [docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md)。

### ゲート付きの自己改善

mini-ork は同じレシピグラフを通じて mini-ork 自身を編集します。`framework-edit`（ブラインドのスペックテストで検証される自己編集）、`self-improve`（スキャン、レンズ、統合、実装、ゲート）、`rsi-technique-review`（論文を読み、実装すべき手法を提案）、そして `goal-loop` です。学習による変更は、ハッキング可能性の監査と検証器の監査を通過したうえで、ブラインド再実行の対照群と比較する凍結プローブ上の非退行ゲートを通らなければなりません。昇格したバージョンにはすべてロールバック用のポインタが残ります。プロンプトを無人で書き換える適用ループは**デフォルトで無効**の状態で出荷しています（下の警告を参照）。`framework-edit` は最も多く実行されているレシピで、`self-improve` は 2026-10-23 に自分自身の修正を見つけてマージしました。詳細: [docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md)、[docs/SAFETY.md](docs/SAFETY.md)、[docs/ECOSYSTEM.md](docs/ECOSYSTEM.md)。

## よく使うコマンド

| コマンド | 内容 |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | レシピを 1 つ最初から最後まで実行 |
| `mini-ork doctor` | 依存関係、環境、プロバイダーの事前確認をチェック |
| `mini-ork validate` | `Fix:` ヒント付きの実行前静的チェック |
| `mini-ork providers status <lane>` | 認証情報の有無と取得元を表示（値は表示しない） |
| `mini-ork serve` | `127.0.0.1:7090` でローカル Web UI と API を提供 |
| `mini-ork garden` | ドリフト検出: 衝突、孤立したもの、古くなった実行 |
| `mini-ork recover` / `resume` | 失敗または中断した実行を復活 |
| `mini-ork mcp` / `vault` | MCP サーバーのレジストリ、シークレット保管庫のバックエンドチェーン |
| `mini-ork zed setup` / `cn status` | エディター連携、ContextNest のティアと到達性 |

最初に試すレシピ: `code-fix`、`docs`、`refactor-audit`、`bug-audit-cmgk`、`research-synthesis`、`spec-driven-delivery`。[recipes/](recipes/) と[拡張ガイド](docs/EXTENSION.md)を参照してください。

## アップグレード

インストーラーを再実行するか、チェックアウトから `make install` を実行します。何度実行しても安全です。`.venv` を再利用し、editable パッケージを更新し、管理対象のコマンドを修復し、OS ツールを検証します。続けて `mini-ork update` を実行すると、保留中のデータベースマイグレーションを適用し、設定のドリフトを報告します。

## アンインストール

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

プロジェクトの状態は各プロジェクトの `.mini-ork/` にあります。不要になったプロジェクトでは削除してください。ソースのチェックアウトや git の履歴に手が加わることはありません。

## 動作要件

| | |
|---|---|
| OS | macOS、Linux、WSL。ネイティブ Windows の場合は、`winget` で Python 3.11、Git、jq、yq、SQLite をインストールしてから `py -3 .\scripts\full_install.py` を実行 |
| Python | 3.11 または 3.12 |
| Git | 必須 |
| Docker | サンドボックスでの検証（`certify`、Crucible）にのみ必要 |
| Rust 1.80+ | ContextNest と IDE クレートにのみ必要 |
| モデルへのアクセス | 実際の実行には設定済みのプロバイダーレーンが 1 つ必要。ドライランでは不要 |

## さらに詳しく

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | プロジェクトが存在する理由、計測結果、主張していないこと |
| [docs/SAFETY.md](docs/SAFETY.md) | 自己改変に対する姿勢と、それを支えるゲート |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | システム設計とコンポーネント図 |
| [docs/operator](docs/operator) | mini-ork の運用: 環境変数、トラブルシューティング |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | mini-ork を自分のアプリに組み込む |
| [docs/RESULTS.md](docs/RESULTS.md) | 数値の裏付けとなる手法とサンプルサイズ |
| [ROADMAP.md](ROADMAP.md) | プロジェクトの今後の方向性 |

> [!WARNING]
> mini-ork には、自身のプロンプトとワークフローを書き換え、人間のレビューなしで変更を昇格できる適用ループが含まれています。これは**デフォルトで無効**です（`MO_APPLY_ENABLED` と `MO_AUTO_APPLY` の両方を `1` にする必要があります）。影響範囲、上限、ロールバックについては [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended) に記載しています。

## コントリビューションとライセンス

mini-ork のライセンスは **Apache-2.0** です（© Amir Khakshour）。まだ初期段階の研究用ソフトウェアです。フレームワークを変更するときは専用の worktree を使い、挙動に関する主張には必ず検証器を添え、変更した範囲に絞ったチェックを実行してください。コントリビューションの手順は [AGENTS.md](AGENTS.md)、プロジェクトの方針は [GOVERNANCE.md](GOVERNANCE.md) を参照してください。

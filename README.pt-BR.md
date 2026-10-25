
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>Um sistema operacional de tarefas para agentes de IA — que os obriga a provar o próprio trabalho.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | <a href="README.ko.md">한국어</a> | <a href="README.es.md">Español</a> | Português (Brasil) | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.tr.md">Türkçe</a> | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="Uma execução de code-fix na IDE do mini-ork: classificar, planejar, implementar, verificar, três lentes de revisão, publicar e, por fim, Keep ou Reject para cada arquivo alterado" width="900">
  </a>
</p>
<p align="center"><sub>Uma execução de <code>code-fix</code> na IDE do mini-ork: classificar → planejar → implementar → verificar → três lentes de revisão em três famílias de modelos → publicar; depois, cada arquivo alterado aguarda Keep ou Reject. Execução de exemplo do <a href="https://miniork.dev/">passo a passo</a> · <a href="https://miniork.dev/media/mini-ork-demo.mp4">vídeo em qualidade total</a>.</sub></p>

O mini-ork transforma um objetivo em uma execução planejada, realizada e **verificada** em uma
frota de modelos: **classificar → planejar → executar → verificar → refletir → melhorar**. Todo
veredito vem do que o código realmente fez ao rodar — testes, checagem de tipos, schemas,
execução em um sandbox isolado —, e não da opinião de um modelo sobre a própria saída.

Esta página é o guia de instalação. Para entender a motivação do projeto, veja
[Por que o mini-ork existe](docs/WHY.md).

## Início rápido

macOS, Linux ou WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

O instalador:

- clona o mini-ork em `~/.local/share/mini-ork` (`MINI_ORK_INSTALL_DIR` muda esse caminho),
- cria um `.venv` local ao checkout e instala o perfil Python completo
  (CLI, sidecar web local, Crucible),
- coloca `mini-ork` no seu PATH em `~/.local/bin`,
- oferece o assistente de primeira execução quando roda em um terminal.

A partir de um checkout existente, a mesma instalação:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` preenche `ui/`, o crate da IDE
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)). O runtime
Python não depende dele; veja [docs/UI.md](docs/UI.md) para compilar a IDE.

## Primeira execução: `mini-ork onboard`

Um único comando leva uma máquina recém-configurada a um workspace pronto para rodar uma
receita. Ele nunca chama um modelo nem gasta dinheiro: cada etapa é uma verificação ou um
build local.

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| Etapa | O que acontece |
|---|---|
| prerequisites | Verifica Python ≥ 3.11 e git; registra `sqlite3` / `jq` se estiverem presentes |
| workspace | Cria `.mini-ork/` no diretório atual (roda `git init` se necessário) |
| provider lanes | Informa se há credenciais armazenadas ou indica o comando que as adiciona |
| contextnest | *Opcional:* clona, compila e gera uma configuração inicial para o serviço de memória local |
| zed | *Opcional:* conecta o agente e os servidores MCP do mini-ork ao `settings.json` do Zed |

Os componentes opcionais só são oferecidos quando o toolchain correspondente está presente
(`cargo`, `zed`), e cada um pode ser escolhido explicitamente:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

Códigos de saída: `0` concluído (componentes pulados não contam como falha), `1` um
componente selecionado falhou com `--strict`, `2` erro de uso. Uma execução não interativa
não faz perguntas: `-y` aceita todos os padrões.

### ContextNest — substrato de memória local

Um serviço HTTP em Rust que lembra o que sessões anteriores decidiram, onde falharam, o que
verificaram e o que entregaram. O assistente gera `config/contextnest.yaml` no nível
**simples**; com `--secure`, não grava nada e exige `CN_TOKEN` antes de o serviço subir.

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

Manualmente: `git clone https://github.com/SourceShift/ContextNest.git` e depois
`make cn-serve` (escuta em `127.0.0.1:28080`). Requer Rust 1.80+. Detalhes:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md).

### Zed — integração com o editor

Conecta `mini-ork acp` (agente externo) e `mini-ork mcp-context` (contexto somente leitura
de execuções, aprendizado e custos) ao Zed. Toda escrita no `settings.json` é precedida de
um backup com timestamp.

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

Veja [docs/ZED.md](docs/ZED.md) e [docs/UI.md](docs/UI.md).

## Seu primeiro fluxo verificado

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

Um kickoff define o repositório-alvo, os arquivos permitidos, o artefato esperado e o
comando ou a regra que comprova o sucesso. Os artefatos da execução ficam em
`.mini-ork/runs/`, e o estado registrado, em `.mini-ork/state.db`. Revise
`.mini-ork/config/agents.yaml` para mudar qual lane atende cada papel.

### Prove uma correção com um único comando

`mini-ork certify` recebe uma mudança e o bug que ela diz corrigir, reproduz o bug no seu
código, gera entradas para as quais a mudança não foi escrita, roda tudo em um sandbox
Docker e devolve um veredito com as evidências anexadas:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

Códigos de saída: `0` PROVEN, `1` REFUTED, `2` UNVERIFIED — ele se abstém em vez de chutar,
então o CI pode usá-lo como gate. Precisa de Docker e de uma lane de modelo.
Experimente `bash examples/certify-demo/demo.sh`.

## Verificação, aprendizado e autoaperfeiçoamento

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="O mapa de zonas em miniork.dev: o loop de execução, o loop de aprendizado, o loop de reparo e o loop de RSI acendem um de cada vez, cada um mostrando o que flui entre o motor, a verificação, o aprendizado e a recuperação" width="900">
  </a>
</p>
<p align="center"><sub>Quatro loops em torno de um único motor, do <a href="https://miniork.dev/">passo a passo</a> (dados de exemplo) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">vídeo em qualidade total</a>.</sub></p>

O GIF no topo desta página é o loop de execução. Outras três camadas ficam em volta dele.

### Verificação que os agentes não conseguem enrolar

Todo veredito é evidência de execução. Os verificadores de uma receita rodam uma proteção
contra adulteração, a suíte de testes, um replay na base (o teste precisa falhar no código
antigo) e uma checagem de adequação por mutação. Uma segunda lane de modelo escreve testes
de especificação cegos a partir do kickoff antes de o implementador começar, e o
implementador não pode editá-los. A cadeia de gates do publicador recusa tudo o que não
passou, e um modelo revisor pode vetar um resultado, mas nunca criar uma aprovação. O
`certify` aplica a mesma postura a uma única mudança e responde PROVEN, REFUTED ou, quando
não consegue provar nem uma coisa nem outra, UNVERIFIED. Em um corpus adversarial do
SWE-bench, 21 de 21 vereditos PROVEN estavam corretos, com 0 falsas conclusões; o método e
seus limites estão em [docs/RESULTS.md](docs/RESULTS.md), e os mecanismos, em
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md).

### Aprendizado que só guarda o que foi verificado

Depois de cada execução, os traces viram uma recompensa de processo por nó, e uma recompensa
positiva precisa vir acompanhada de evidência do verificador. A partir daí, o loop se
ramifica: vantagens por lane com as quais o roteador aprende (GRPO), gradientes textuais dos
quais a etapa de reflexão extrai lições e uma checagem de conflito antes de qualquer lição
ser gravada. Só lições verificadas chegam a um prompt, todo prompt carrega o bloco
aprendido, e um registro de injeção anota exatamente o que cada nó recebeu, para que o
crédito volte à lição que o mereceu. Hoje os gradientes e os temas verificados já chegam aos
prompts, enquanto o roteamento aprendido ainda decide uma pequena minoria das rotas. Ciclo
de vida e status:
[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md).

### Autoaperfeiçoamento, com gates

O mini-ork edita o próprio mini-ork pelo mesmo grafo de receitas: `framework-edit`
(autoedições verificadas com testes de especificação cegos), `self-improve` (varredura,
lentes, síntese, implementação, gates), `rsi-technique-review` (lê a literatura e propõe
técnicas para implementar) e `goal-loop`. Uma mudança aprendida precisa passar por uma
auditoria de hackeabilidade e por uma auditoria do verificador e, depois, por um gate de não
regressão em probes congeladas contra um braço de controle com reexecução cega; toda versão
promovida guarda um ponteiro de rollback. O loop de aplicação que reescreve prompts sem
supervisão vem **desativado por padrão** (veja o aviso abaixo). `framework-edit` é a receita
mais executada, e `self-improve` encontrou e fez merge da própria correção em 2026-10-23.
Detalhes:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

## Comandos do dia a dia

| Comando | O que faz |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | Executa uma receita de ponta a ponta |
| `mini-ork doctor` | Verifica dependências, ambiente e preflight dos provedores |
| `mini-ork validate` | Checagens estáticas pré-execução com dicas `Fix:` |
| `mini-ork providers status <lane>` | Mostra se a credencial existe e de onde vem (nunca os valores) |
| `mini-ork serve` | UI web e API locais em `127.0.0.1:7090` |
| `mini-ork garden` | Detecção de drift: colisões, órfãos, execuções obsoletas |
| `mini-ork recover` / `resume` | Ressuscita uma execução que falhou ou foi interrompida |
| `mini-ork mcp` / `vault` | Registro de servidores MCP; cadeia de backends do cofre de segredos |
| `mini-ork zed setup` / `cn status` | Integração com o editor; nível e alcançabilidade do ContextNest |

Receitas para começar: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`. Veja [recipes/](recipes/) e o
[guia de extensão](docs/EXTENSION.md).

## Atualização

Rode o instalador de novo ou, a partir de um checkout, rode `make install` — é seguro
repetir: ele reaproveita o `.venv`, atualiza o pacote editável, repara o comando gerenciado e
verifica as ferramentas do sistema operacional. Depois, `mini-ork update` aplica as migrações
de banco de dados pendentes e informa o drift de configuração.

## Desinstalação

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

O estado de cada projeto fica no `.mini-ork/` dele; apague-o onde não quiser mais.
Seus checkouts de código-fonte e o histórico do git nunca são tocados.

## Requisitos

| | |
|---|---|
| SO | macOS, Linux, WSL. Windows nativo: instale Python 3.11, Git, jq, yq e SQLite com `winget` e depois rode `py -3 .\scripts\full_install.py` |
| Python | 3.11 ou 3.12 |
| Git | obrigatório |
| Docker | só para verificação em sandbox (`certify`, Crucible) |
| Rust 1.80+ | só para o ContextNest e o crate da IDE |
| Acesso a modelos | uma lane de provedor configurada para execuções reais; dry runs não precisam de nenhuma |

## Saiba mais

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | Por que o projeto existe; medições; o que ele não afirma |
| [docs/SAFETY.md](docs/SAFETY.md) | Postura sobre automodificação e os gates que a mantêm sob controle |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design do sistema e diagramas de componentes |
| [docs/operator](docs/operator) | Como operar o mini-ork: variáveis de ambiente, solução de problemas |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | Incorpore o mini-ork na sua própria aplicação |
| [docs/RESULTS.md](docs/RESULTS.md) | Métodos e tamanhos de amostra por trás dos números |
| [ROADMAP.md](ROADMAP.md) | Para onde o projeto está indo |

> [!WARNING]
> O mini-ork contém um loop de aplicação capaz de reescrever os próprios prompts e workflows e
> de promover a mudança sem revisão humana. Ele vem **desativado por padrão** (tanto
> `MO_APPLY_ENABLED` quanto `MO_AUTO_APPLY` precisam ser `1`), e seu raio de impacto, limites e
> rollbacks estão documentados em [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended).

## Contribuição e licença

O mini-ork é **Apache-2.0**, © Amir Khakshour. É um projeto em estágio inicial, de nível de
pesquisa: use um worktree dedicado para mudanças no framework, acompanhe toda afirmação sobre
comportamento com um verificador e rode as checagens focadas na superfície que você alterar.
Veja [AGENTS.md](AGENTS.md) para o fluxo de contribuição e [GOVERNANCE.md](GOVERNANCE.md)
para a direção do projeto.

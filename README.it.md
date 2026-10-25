
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>Un sistema operativo per i task degli agenti AI, che li costringe a dimostrare il proprio lavoro.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | <a href="README.ko.md">한국어</a> | <a href="README.es.md">Español</a> | <a href="README.pt-BR.md">Português (Brasil)</a> | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | <a href="README.hi.md">हिन्दी</a> | Italiano | <a href="README.tr.md">Türkçe</a> | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="Un'esecuzione code-fix nell'IDE di mini-ork: classificazione, pianificazione, implementazione, verifica, tre prospettive di revisione, pubblicazione, poi Keep o Reject per ogni file modificato" width="900">
  </a>
</p>
<p align="center"><sub>Un'esecuzione <code>code-fix</code> nell'IDE di mini-ork: classificazione → pianificazione → implementazione → verifica → tre prospettive di revisione su tre famiglie di modelli → pubblicazione, poi ogni file modificato attende Keep o Reject. Esecuzione di esempio dalla <a href="https://miniork.dev/">demo guidata</a> · <a href="https://miniork.dev/media/mini-ork-demo.mp4">video in qualità piena</a>.</sub></p>

mini-ork trasforma un obiettivo in un lavoro pianificato, eseguito e **verificato** da una flotta
di modelli: **classificazione → pianificazione → esecuzione → verifica → riflessione → miglioramento**.
Ogni verdetto nasce da ciò che il codice ha fatto davvero quando è stato eseguito (test, controlli
di tipo, schemi, esecuzione in una sandbox isolata), non dall'opinione di un modello sul proprio output.

Questa pagina è la guida all'installazione. Per le ragioni alla base del progetto, vedi
[Perché esiste mini-ork](docs/WHY.md).

## Avvio rapido

macOS, Linux o WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

L'installer:

- clona mini-ork in `~/.local/share/mini-ork` (si cambia con `MINI_ORK_INSTALL_DIR`),
- crea un `.venv` locale al checkout e installa il profilo Python completo
  (CLI, sidecar web locale, Crucible),
- aggiunge `mini-ork` al PATH in `~/.local/bin`,
- propone la procedura guidata di primo avvio quando viene eseguito in un terminale.

Da un checkout esistente, la stessa installazione:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` popola `ui/`, il crate dell'IDE
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)). Il runtime
Python non ne ha bisogno; per compilare l'IDE vedi [docs/UI.md](docs/UI.md).

## Primo avvio: `mini-ork onboard`

Un solo comando porta una macchina appena configurata a un workspace pronto per eseguire una
ricetta. Non chiama mai un modello e non spende mai denaro: ogni passo è un controllo locale o
una build locale.

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| Passo | Cosa succede |
|---|---|
| prerequisiti | Verifica Python ≥ 3.11 e git; segnala `sqlite3` / `jq` se presenti |
| workspace | Crea `.mini-ork/` nella directory corrente (esegue `git init` se serve) |
| lane dei provider | Indica se le credenziali sono salvate, oppure il comando che le aggiunge |
| contextnest | *Facoltativo:* clona, compila e prepara una configurazione per il servizio di memoria locale |
| zed | *Facoltativo:* collega l'agente e i server MCP di mini-ork al `settings.json` di Zed |

I componenti facoltativi vengono proposti solo se la loro toolchain è presente (`cargo`, `zed`),
e ciascuno si può scegliere esplicitamente:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

Codici di uscita: `0` completato (i componenti saltati non sono un problema), `1` un componente
selezionato è fallito con `--strict`, `2` errore d'uso. Un'esecuzione non interattiva non pone
domande: `-y` accetta tutti i valori predefiniti.

### ContextNest — substrato di memoria locale

Un servizio HTTP in Rust che ricorda cosa le sessioni passate hanno deciso, verificato e
rilasciato, e dove hanno fallito. La procedura guidata prepara `config/contextnest.yaml` nel
livello **semplice**; con `--secure` non scrive nulla e richiede `CN_TOKEN` prima che il servizio
possa partire.

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

A mano: `git clone https://github.com/SourceShift/ContextNest.git`, poi
`make cn-serve` (in ascolto su `127.0.0.1:28080`). Richiede Rust 1.80+. Dettagli:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md).

### Zed — integrazione con l'editor

Collega a Zed `mini-ork acp` (agente esterno) e `mini-ork mcp-context` (contesto in sola
lettura su esecuzioni, apprendimento e costi). Ogni scrittura su `settings.json` è preceduta da
un backup con timestamp.

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

Vedi [docs/ZED.md](docs/ZED.md) e [docs/UI.md](docs/UI.md).

## Il tuo primo workflow verificato

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

Un kickoff indica il repository di destinazione, i file consentiti, l'artefatto atteso e il
comando o la regola che dimostra il successo. Gli artefatti dell'esecuzione finiscono in
`.mini-ork/runs/`, lo stato registrato in `.mini-ork/state.db`. Consulta
`.mini-ork/config/agents.yaml` per cambiare quale lane copre quale ruolo.

### Dimostrare una correzione con un solo comando

`mini-ork certify` prende una modifica e il bug che dichiara di correggere, riproduce il bug
sul tuo codice, genera input per cui la modifica non è stata scritta, esegue tutto in una
sandbox Docker e restituisce un verdetto con le prove allegate:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

Codici di uscita `0` PROVEN, `1` REFUTED, `2` UNVERIFIED: anziché tirare a indovinare si
astiene, quindi la CI può usarlo come gate. Servono Docker e una lane di modello.
Prova `bash examples/certify-demo/demo.sh`.

## Verifica, apprendimento e auto-miglioramento

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="La mappa delle zone su miniork.dev: il ciclo di esecuzione, il ciclo di apprendimento, il ciclo di riparazione e il ciclo RSI si illuminano a turno, ciascuno mostrando cosa passa tra motore, verifica, apprendimento e recupero" width="900">
  </a>
</p>
<p align="center"><sub>Quattro cicli attorno a un unico motore, dalla <a href="https://miniork.dev/">demo guidata</a> (dati di esempio) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">video in qualità piena</a>.</sub></p>

La GIF in cima a questa pagina è il ciclo di esecuzione. Attorno a esso ci sono altri tre livelli.

### Una verifica che gli agenti non possono aggirare a parole

Ogni verdetto è una prova di esecuzione. I verificatori di una ricetta eseguono una protezione
anti-manomissione, la suite di test, un replay sulla base (il test deve fallire sul vecchio
codice) e un controllo di adeguatezza tramite mutazioni. Una seconda lane di modello scrive test
di specifica alla cieca partendo dal kickoff, prima che l'implementatore inizi, e l'implementatore
non può modificarli. La catena di gate del publisher rifiuta tutto ciò che non è passato, e un
modello revisore può porre il veto su un risultato ma non può mai decretarne il superamento.
`certify` applica lo stesso principio a una singola modifica e risponde PROVEN, REFUTED, oppure
UNVERIFIED quando non può dimostrare né l'una né l'altra cosa. Su un corpus SWE-bench
avversariale, 21 verdetti PROVEN su 21 erano corretti, con 0 falsi completamenti; il metodo e i
suoi limiti sono in [docs/RESULTS.md](docs/RESULTS.md), i meccanismi in
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md).

### Un apprendimento che conserva solo ciò che è verificato

Dopo ogni esecuzione le tracce diventano una ricompensa di processo per nodo, e una ricompensa
positiva deve essere accompagnata da prove del verificatore. Da lì il ciclo si dirama: vantaggi
per lane da cui il router impara (GRPO), gradienti testuali che la fase di riflessione trasforma
in lezioni, e un controllo dei conflitti prima che una lezione venga scritta. Solo le lezioni
verificate arrivano a un prompt, ogni prompt include il blocco appreso e un registro delle
iniezioni annota esattamente cosa ha ricevuto ogni nodo, così il merito risale alla lezione che
lo ha guadagnato. Oggi gradienti e temi verificati arrivano davvero nei prompt, mentre il routing
appreso decide ancora solo una piccola minoranza degli instradamenti. Ciclo di vita e stato:
[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md).

### Auto-miglioramento, con gate

mini-ork modifica mini-ork attraverso lo stesso grafo di ricette: `framework-edit` (auto-modifiche
verificate con test di specifica alla cieca), `self-improve` (scansione, prospettive, sintesi,
implementazione, gate), `rsi-technique-review` (legge la letteratura e propone tecniche da
realizzare) e `goal-loop`. Una modifica appresa deve superare un audit di aggirabilità e un audit
dei verificatori, poi un gate di non regressione su probe congelate rispetto a un braccio di
controllo con riesecuzione alla cieca, e ogni versione promossa conserva un puntatore di rollback.
Il ciclo di applicazione che riscrive i prompt senza supervisione è **disattivato di default**
(vedi l'avviso più sotto). `framework-edit` è la ricetta più eseguita, e il 2026-10-23
`self-improve` ha trovato la propria correzione e l'ha integrata. Dettagli:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

## Comandi di tutti i giorni

| Comando | Cosa fa |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | Esegue una ricetta dall'inizio alla fine |
| `mini-ork doctor` | Verifica dipendenze, ambiente e preflight dei provider |
| `mini-ork validate` | Controlli statici pre-esecuzione con suggerimenti `Fix:` |
| `mini-ork providers status <lane>` | Mostra presenza e origine delle credenziali (mai i valori) |
| `mini-ork serve` | UI web e API locali su `127.0.0.1:7090` |
| `mini-ork garden` | Rilevamento delle derive: collisioni, orfani, esecuzioni obsolete |
| `mini-ork recover` / `resume` | Riporta in vita un'esecuzione fallita o interrotta |
| `mini-ork mcp` / `vault` | Registro dei server MCP; catena di backend del vault dei segreti |
| `mini-ork zed setup` / `cn status` | Collegamento all'editor; livello e raggiungibilità di ContextNest |

Ricette di partenza: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`. Vedi [recipes/](recipes/) e la
[guida alle estensioni](docs/EXTENSION.md).

## Aggiornamento

Riesegui l'installer, oppure da un checkout esegui `make install`: rieseguirlo è sicuro, perché
riusa `.venv`, aggiorna il pacchetto editable, ripara il comando gestito e verifica gli strumenti
del sistema operativo. Poi `mini-ork update` applica le migrazioni del database in sospeso e
segnala le derive di configurazione.

## Disinstallazione

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

Lo stato di ogni progetto vive nella sua directory `.mini-ork/`; eliminala dove non ti serve più.
I tuoi checkout dei sorgenti e la cronologia git non vengono mai toccati.

## Requisiti

| | |
|---|---|
| Sistema operativo | macOS, Linux, WSL. Windows nativo: installa Python 3.11, Git, jq, yq e SQLite con `winget`, poi esegui `py -3 .\scripts\full_install.py` |
| Python | 3.11 o 3.12 |
| Git | obbligatorio |
| Docker | solo per la verifica in sandbox (`certify`, Crucible) |
| Rust 1.80+ | solo per ContextNest e il crate dell'IDE |
| Accesso ai modelli | una lane di provider configurata per le esecuzioni reali; le dry run non ne richiedono |

## Per saperne di più

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | Perché esiste il progetto; misurazioni; cosa non afferma |
| [docs/SAFETY.md](docs/SAFETY.md) | Posizione sull'auto-modifica e i gate che la contengono |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Progettazione del sistema e diagrammi dei componenti |
| [docs/operator](docs/operator) | Usare mini-ork: variabili d'ambiente, risoluzione dei problemi |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | Integra mini-ork nella tua applicazione |
| [docs/RESULTS.md](docs/RESULTS.md) | Metodi e dimensioni dei campioni dietro i numeri |
| [ROADMAP.md](ROADMAP.md) | Dove sta andando il progetto |

> [!WARNING]
> mini-ork contiene un ciclo di applicazione che può riscrivere i propri prompt e workflow e
> promuovere la modifica senza revisione umana. È **disattivato di default** (sia
> `MO_APPLY_ENABLED` sia `MO_AUTO_APPLY` devono valere `1`), e il suo raggio d'impatto, i limiti e
> i rollback sono documentati in [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended).

## Contribuire e licenza

mini-ork è rilasciato sotto licenza **Apache-2.0**, © Amir Khakshour. È in fase iniziale e di
livello sperimentale: per le modifiche al framework usa un worktree dedicato, accompagna ogni
affermazione sul comportamento con un verificatore ed esegui i controlli mirati per la parte che
modifichi. Vedi [AGENTS.md](AGENTS.md) per il flusso di contribuzione e
[GOVERNANCE.md](GOVERNANCE.md) per la direzione del progetto.

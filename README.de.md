
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>Ein Task-Betriebssystem für KI-Agenten, das von ihnen verlangt, ihre Arbeit zu beweisen.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | <a href="README.ko.md">한국어</a> | <a href="README.es.md">Español</a> | <a href="README.pt-BR.md">Português (Brasil)</a> | Deutsch | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.tr.md">Türkçe</a> | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="Ein code-fix-Lauf in der mini-ork-IDE: klassifizieren, planen, implementieren, verifizieren, drei Review-Perspektiven, veröffentlichen, danach Keep oder Reject für jede geänderte Datei" width="900">
  </a>
</p>
<p align="center"><sub>Ein <code>code-fix</code>-Lauf in der mini-ork-IDE: klassifizieren → planen → implementieren → verifizieren → drei Review-Perspektiven auf drei Modellfamilien → veröffentlichen; danach wartet jede geänderte Datei auf Keep oder Reject. Beispiellauf aus dem <a href="https://miniork.dev/">Walkthrough</a> · <a href="https://miniork.dev/media/mini-ork-demo.mp4">Video in voller Qualität</a>.</sub></p>

mini-ork macht aus einem Ziel einen geplanten, ausgeführten und **verifizierten** Lauf über
eine ganze Flotte von Modellen: **klassifizieren → planen → ausführen → verifizieren →
reflektieren → verbessern**. Jedes Urteil beruht darauf, was der Code bei der Ausführung
tatsächlich getan hat – Tests, Typprüfungen, Schemas, Ausführung in einer isolierten
Sandbox –, nicht auf der Meinung eines Modells über seine eigene Ausgabe.

Diese Seite ist die Installationsanleitung. Die Begründung hinter dem Projekt finden Sie
unter [Warum es mini-ork gibt](docs/WHY.md).

## Schnellstart

macOS, Linux oder WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

Der Installer:

- klont mini-ork nach `~/.local/share/mini-ork` (überschreibbar mit `MINI_ORK_INSTALL_DIR`),
- legt im Checkout ein lokales `.venv` an und installiert das vollständige Python-Profil
  (CLI, lokaler Web-Sidecar, Crucible),
- legt `mini-ork` über `~/.local/bin` auf Ihren PATH,
- bietet den Einrichtungsassistenten an, wenn er in einem Terminal läuft.

Dieselbe Installation aus einem vorhandenen Checkout:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` befüllt `ui/`, das IDE-Crate
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)). Die
Python-Laufzeit braucht es nicht; wie Sie die IDE bauen, steht in [docs/UI.md](docs/UI.md).

## Erster Start: `mini-ork onboard`

Ein einziger Befehl macht aus einem frischen Rechner einen Workspace, in dem Sie ein Recipe
ausführen können. Er ruft nie ein Modell auf und kostet nie Geld – jeder Schritt ist eine
lokale Prüfung oder ein lokaler Build.

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| Schritt | Was passiert |
|---|---|
| Voraussetzungen | Prüft Python ≥ 3.11 und git; vermerkt `sqlite3` / `jq`, falls vorhanden |
| Workspace | Legt `.mini-ork/` im aktuellen Verzeichnis an (führt bei Bedarf `git init` aus) |
| Provider-Lanes | Meldet, ob Zugangsdaten hinterlegt sind, oder nennt den Befehl, der sie hinzufügt |
| contextnest | *Optional:* klont und baut den lokalen Memory-Dienst und legt eine Startkonfiguration an |
| zed | *Optional:* bindet den Agenten und die MCP-Server von mini-ork in Zeds `settings.json` ein |

Optionale Komponenten werden nur angeboten, wenn ihre Toolchain vorhanden ist (`cargo`, `zed`),
und jede lässt sich explizit auswählen:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

Exit-Codes: `0` fertig (übersprungene Komponenten sind in Ordnung), `1` eine ausgewählte
Komponente ist unter `--strict` fehlgeschlagen, `2` Aufruffehler. Ein nicht interaktiver Lauf
stellt keine Rückfragen: `-y` übernimmt alle Standardwerte.

### ContextNest – lokales Memory-Substrat

Ein Rust-HTTP-Dienst, der sich merkt, was frühere Sessions entschieden, verifiziert und
ausgeliefert haben und woran sie gescheitert sind. Der Assistent legt `config/contextnest.yaml`
im **simple**-Tier an; mit `--secure` schreibt er nichts und verlangt `CN_TOKEN`, bevor der
Dienst startet.

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

Von Hand: `git clone https://github.com/SourceShift/ContextNest.git`, danach
`make cn-serve` (lauscht auf `127.0.0.1:28080`). Erfordert Rust 1.80+. Details:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md).

### Zed – Editor-Integration

Bindet `mini-ork acp` (externer Agent) und `mini-ork mcp-context` (schreibgeschützter
Kontext zu Läufen, Lernen und Kosten) in Zed ein. Vor jedem Schreibzugriff auf
`settings.json` wird ein Backup mit Zeitstempel angelegt.

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

Siehe [docs/ZED.md](docs/ZED.md) und [docs/UI.md](docs/UI.md).

## Ihr erster verifizierter Workflow

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

Ein Kickoff nennt das Ziel-Repository, die erlaubten Dateien, das angestrebte Artefakt und
den Befehl oder die Regel, die den Erfolg belegt. Lauf-Artefakte landen in `.mini-ork/runs/`,
der aufgezeichnete Zustand in `.mini-ork/state.db`. Welche Lane welche Rolle übernimmt,
legen Sie in `.mini-ork/config/agents.yaml` fest.

### Einen Fix mit einem Befehl beweisen

`mini-ork certify` nimmt eine Änderung und den Bug, den sie angeblich behebt, reproduziert
den Bug gegen Ihren Code, erzeugt Eingaben, für die die Änderung nicht geschrieben wurde,
führt alles in einer Docker-Sandbox aus und liefert ein Urteil samt Belegen:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

Exit-Codes `0` PROVEN, `1` REFUTED, `2` UNVERIFIED – im Zweifel enthält es sich, statt zu
raten, sodass CI das Ergebnis als Gate nutzen kann. Benötigt Docker und eine Modell-Lane.
Probieren Sie `bash examples/certify-demo/demo.sh` aus.

## Verifikation, Lernen und Selbstverbesserung

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="Die Zonenkarte auf miniork.dev: Lauf-, Lern-, Reparatur- und RSI-Schleife leuchten nacheinander auf; jede zeigt, was zwischen Engine, Verifikation, Lernen und Wiederherstellung fließt" width="900">
  </a>
</p>
<p align="center"><sub>Vier Schleifen um eine Engine, aus dem <a href="https://miniork.dev/">Walkthrough</a> (Beispieldaten) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">Video in voller Qualität</a>.</sub></p>

Das GIF oben auf dieser Seite zeigt die Lauf-Schleife. Drei weitere Ebenen legen sich darum.

### Verifikation, an der sich die Agenten nicht vorbeireden können

Jedes Urteil ist ein Ausführungsbeleg. Die Verifier eines Recipes führen einen
Manipulationsschutz, die Testsuite, ein Replay auf dem Basisstand (der Test muss am alten
Code scheitern) und eine Mutations-Adäquanzprüfung aus. Eine zweite Modell-Lane schreibt
aus dem Kickoff blinde Spec-Tests, bevor der Implementer startet, und der Implementer kann
sie nicht ändern. Die Gate-Kette des Publishers weist alles zurück, was nicht bestanden hat;
ein Reviewer-Modell kann ein Ergebnis per Veto stoppen, aber niemals ein Bestehen erzeugen.
`certify` wendet dieselbe Haltung auf eine einzelne Änderung an und antwortet mit PROVEN,
REFUTED oder UNVERIFIED, wenn es weder das eine noch das andere beweisen kann. Auf einem
adversarialen SWE-bench-Korpus waren 21 von 21 PROVEN-Urteilen korrekt, bei 0 falschen
Fertigmeldungen; Methode und Grenzen stehen in [docs/RESULTS.md](docs/RESULTS.md), die
Mechanismen in
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md).

### Lernen, das nur behält, was verifiziert ist

Nach jedem Lauf werden die Traces zu einem Prozess-Reward pro Knoten, und ein positiver
Reward muss mit Verifier-Belegen gepaart sein. Von dort fächert sich die Schleife auf:
Advantages pro Lane, aus denen der Router lernt (GRPO), Textgradienten, die der
Reflexionsschritt zu Lektionen verdichtet, und eine Konfliktprüfung, bevor überhaupt eine
Lektion geschrieben wird. Nur verifizierte Lektionen gelangen in einen Prompt, jeder Prompt
enthält den gelernten Block, und ein Injection-Ledger hält genau fest, was jeder Knoten
erhalten hat, sodass der Credit an die Lektion zurückfließt, die ihn verdient hat. Heute
fließen die Gradienten und die verifizierten Themen tatsächlich in Prompts ein, während
gelerntes Routing erst über eine kleine Minderheit der Routen entscheidet. Lebenszyklus und
Status: [docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md).

### Selbstverbesserung, durch Gates abgesichert

mini-ork bearbeitet mini-ork über denselben Recipe-Graphen: `framework-edit` (verifizierte
Selbst-Edits mit blinden Spec-Tests), `self-improve` (Scan, Perspektiven, Synthese,
Implementierung, Gates), `rsi-technique-review` (liest die Fachliteratur und schlägt
Techniken zur Umsetzung vor) und `goal-loop`. Eine gelernte Änderung muss ein
Hackability-Audit und ein Verifier-Audit bestehen, danach ein Regressions-Gate auf
eingefrorenen Probes, gemessen gegen einen Kontrollarm mit blindem Rerun; jede promotete
Version behält einen Rollback-Zeiger. Die Apply-Schleife, die Prompts unbeaufsichtigt
umschreibt, ist **standardmäßig deaktiviert** (siehe Warnung unten). `framework-edit` ist das
am häufigsten ausgeführte Recipe, und `self-improve` hat am 2026-10-23 seinen eigenen Fix
gefunden und gemergt. Details:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

## Befehle für den Alltag

| Befehl | Funktion |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | Führt ein Recipe von Anfang bis Ende aus |
| `mini-ork doctor` | Prüft Abhängigkeiten, Umgebung und Provider-Preflight |
| `mini-ork validate` | Statische Prüfungen vor dem Lauf, mit `Fix:`-Hinweisen |
| `mini-ork providers status <lane>` | Zeigt, ob Zugangsdaten vorhanden sind und woher sie stammen (nie die Werte) |
| `mini-ork serve` | Lokale Web-UI und API auf `127.0.0.1:7090` |
| `mini-ork garden` | Drift-Erkennung: Kollisionen, verwaiste Einträge, veraltete Läufe |
| `mini-ork recover` / `resume` | Holt einen fehlgeschlagenen oder unterbrochenen Lauf zurück |
| `mini-ork mcp` / `vault` | MCP-Server-Registry; Backend-Kette des Secret-Vaults |
| `mini-ork zed setup` / `cn status` | Editor-Anbindung; ContextNest-Tier und Erreichbarkeit |

Recipes für den Einstieg: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`. Siehe [recipes/](recipes/) und den
[Leitfaden zur Erweiterung](docs/EXTENSION.md).

## Upgrade

Führen Sie den Installer erneut aus oder starten Sie in einem Checkout `make install` – ein
erneuter Aufruf ist unbedenklich: Er verwendet `.venv` weiter, aktualisiert das editierbare
Paket, repariert den verwalteten Befehl und prüft die OS-Werkzeuge. Anschließend wendet
`mini-ork update` ausstehende Datenbankmigrationen an und meldet Konfigurationsdrift.

## Deinstallation

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

Der Projektzustand liegt im jeweiligen `.mini-ork/` jedes Projekts; löschen Sie es dort, wo
Sie es nicht mehr brauchen. Ihre Quell-Checkouts und Ihre Git-Historie bleiben unangetastet.

## Systemvoraussetzungen

| | |
|---|---|
| Betriebssystem | macOS, Linux, WSL. Natives Windows: Python 3.11, Git, jq, yq und SQLite mit `winget` installieren, dann `py -3 .\scripts\full_install.py` ausführen |
| Python | 3.11 oder 3.12 |
| Git | erforderlich |
| Docker | nur für Verifikation in der Sandbox (`certify`, Crucible) |
| Rust 1.80+ | nur für ContextNest und das IDE-Crate |
| Modellzugang | eine konfigurierte Provider-Lane für echte Läufe; Dry Runs brauchen keine |

## Weiterlesen

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | Warum es das Projekt gibt; Messungen; was es nicht behauptet |
| [docs/SAFETY.md](docs/SAFETY.md) | Haltung zur Selbstmodifikation und die Gates, die sie absichern |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Systemdesign und Komponentendiagramme |
| [docs/operator](docs/operator) | mini-ork betreiben: Umgebungsvariablen, Fehlerbehebung |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | mini-ork in Ihre eigene Anwendung einbetten |
| [docs/RESULTS.md](docs/RESULTS.md) | Methoden und Stichprobengrößen hinter den Zahlen |
| [ROADMAP.md](ROADMAP.md) | Wohin sich das Projekt entwickelt |

> [!WARNING]
> mini-ork enthält eine Apply-Schleife, die ihre eigenen Prompts und Workflows umschreiben
> und die Änderung ohne menschliches Review promoten kann. Sie ist **standardmäßig
> deaktiviert** (sowohl `MO_APPLY_ENABLED` als auch `MO_AUTO_APPLY` müssen `1` sein);
> Wirkungsradius, Obergrenzen und Rollbacks sind in [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended) dokumentiert.

## Mitwirken und Lizenz

mini-ork steht unter **Apache-2.0**, © Amir Khakshour. Das Projekt ist noch jung und hat
Forschungscharakter: Nutzen Sie für Framework-Änderungen einen eigenen Worktree, belegen Sie
jede Aussage über Verhalten mit einem Verifier und führen Sie die gezielten Prüfungen für den
Bereich aus, den Sie ändern. Den Workflow für Beiträge beschreibt [AGENTS.md](AGENTS.md), die
Ausrichtung des Projekts [GOVERNANCE.md](GOVERNANCE.md).

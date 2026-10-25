
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>Un sistema operativo de tareas para agentes de IA, que los obliga a demostrar su trabajo.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | <a href="README.ko.md">한국어</a> | Español | <a href="README.pt-BR.md">Português (Brasil)</a> | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.tr.md">Türkçe</a> | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="Una ejecución de code-fix en el IDE de mini-ork: clasificar, planificar, implementar, verificar, tres lentes de revisión, publicar y, al final, Keep o Reject para cada archivo modificado" width="900">
  </a>
</p>
<p align="center"><sub>Una ejecución de <code>code-fix</code> en el IDE de mini-ork: clasificar → planificar → implementar → verificar → tres lentes de revisión sobre tres familias de modelos → publicar; después, cada archivo modificado queda a la espera de un Keep o un Reject. Ejecución de ejemplo del <a href="https://miniork.dev/">recorrido guiado</a> · <a href="https://miniork.dev/media/mini-ork-demo.mp4">video en calidad completa</a>.</sub></p>

mini-ork convierte un objetivo en una ejecución que se planifica, se lleva a cabo y se **verifica** sobre una
flota de modelos: **clasificar → planificar → ejecutar → verificar → reflexionar → mejorar**. Cada veredicto
sale de lo que el código hizo realmente al ejecutarse (pruebas, comprobaciones de tipos, esquemas, ejecución
en un sandbox aislado), no de la opinión de un modelo sobre su propia salida.

Esta página es la guía de instalación. Para conocer el razonamiento detrás del proyecto, consulta
[Por qué existe mini-ork](docs/WHY.md).

## Inicio rápido

macOS, Linux o WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

El instalador:

- clona mini-ork en `~/.local/share/mini-ork` (`MINI_ORK_INSTALL_DIR` cambia esa ruta),
- crea un `.venv` dentro de la copia local e instala el perfil completo de Python
  (CLI, sidecar web local, Crucible),
- añade `mini-ork` a tu PATH en `~/.local/bin`,
- ofrece el asistente de primera ejecución cuando corre en una terminal.

Desde una copia local existente, la misma instalación:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` descarga `ui/`, el crate del IDE
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)). El runtime de
Python no lo necesita; consulta [docs/UI.md](docs/UI.md) para compilar el IDE.

## Primera ejecución: `mini-ork onboard`

Un solo comando lleva una máquina recién configurada a un espacio de trabajo listo para ejecutar una receta.
Nunca llama a un modelo ni gasta dinero: cada paso es una comprobación o una compilación local.

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| Paso | Qué ocurre |
|---|---|
| requisitos previos | Comprueba Python ≥ 3.11 y git; indica si `sqlite3` / `jq` están disponibles |
| espacio de trabajo | Crea `.mini-ork/` en el directorio actual (ejecuta `git init` si hace falta) |
| carriles de proveedor | Informa si hay credenciales guardadas o indica el comando que las añade |
| contextnest | *Opcional:* clona, compila y genera una configuración inicial para el servicio de memoria local |
| zed | *Opcional:* conecta el agente y los servidores MCP de mini-ork al `settings.json` de Zed |

Los componentes opcionales solo se ofrecen cuando su cadena de herramientas está instalada (`cargo`, `zed`),
y cada uno puede elegirse de forma explícita:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

Códigos de salida: `0` completado (omitir componentes no es un error), `1` falló un componente seleccionado
con `--strict`, `2` error de uso. Una ejecución no interactiva no hace preguntas: `-y` acepta
todos los valores predeterminados.

### ContextNest — sustrato de memoria local

Un servicio HTTP en Rust que recuerda qué decidieron las sesiones anteriores, en qué fallaron, qué verificaron
y qué entregaron. El asistente genera `config/contextnest.yaml` en el nivel **simple**; con
`--secure` no escribe nada y exige `CN_TOKEN` para que el servicio arranque.

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

A mano: `git clone https://github.com/SourceShift/ContextNest.git` y después
`make cn-serve` (escucha en `127.0.0.1:28080`). Requiere Rust 1.80+. Detalles:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md).

### Zed — integración con el editor

Conecta `mini-ork acp` (agente externo) y `mini-ork mcp-context` (contexto de ejecuciones,
aprendizaje y costos, de solo lectura) con Zed. Antes de cada escritura en `settings.json` se crea
una copia de seguridad con marca de tiempo.

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

Consulta [docs/ZED.md](docs/ZED.md) y [docs/UI.md](docs/UI.md).

## Tu primer flujo de trabajo verificado

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

Un kickoff indica el repositorio de destino, los archivos permitidos, el artefacto esperado y el
comando o la regla que demuestra el éxito. Los artefactos de cada ejecución se guardan en `.mini-ork/runs/`,
y el estado registrado, en `.mini-ork/state.db`. Revisa `.mini-ork/config/agents.yaml` para cambiar qué
carril cubre cada rol.

### Demuestra una corrección con un solo comando

`mini-ork certify` toma un cambio y el bug que dice corregir, reproduce el bug
contra tu código, genera entradas para las que el cambio no se escribió, lo ejecuta todo en
un sandbox de Docker y devuelve un veredicto con la evidencia adjunta:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

Códigos de salida: `0` PROVEN, `1` REFUTED, `2` UNVERIFIED. Prefiere abstenerse antes que adivinar,
así que la CI puede bloquear según su resultado. Necesita Docker y un carril de modelo.
Prueba `bash examples/certify-demo/demo.sh`.

## Verificación, aprendizaje y automejora

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="El mapa de zonas de miniork.dev: el bucle de ejecución, el de aprendizaje, el de reparación y el de RSI se iluminan por turnos, y cada uno muestra lo que circula entre el motor, la verificación, el aprendizaje y la recuperación" width="900">
  </a>
</p>
<p align="center"><sub>Cuatro bucles alrededor de un mismo motor, del <a href="https://miniork.dev/">recorrido guiado</a> (datos de ejemplo) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">video en calidad completa</a>.</sub></p>

El GIF del principio de esta página es el bucle de ejecución. A su alrededor hay tres capas más.

### Una verificación que los agentes no pueden esquivar con palabrería

Cada veredicto es evidencia de ejecución. Los verificadores de una receta ejecutan una protección antimanipulación,
la suite de pruebas, una reejecución sobre la base (la prueba debe fallar con el código antiguo) y una comprobación
de adecuación por mutación. Un segundo carril de modelo escribe pruebas de especificación a ciegas a partir del
kickoff antes de que empiece el implementador, y el implementador no puede editarlas. La cadena de controles del
publicador rechaza todo lo que no haya pasado, y un modelo revisor puede vetar un resultado, pero nunca crear un aprobado.
`certify` aplica el mismo criterio a un único cambio y responde PROVEN, REFUTED o, cuando no puede demostrar
ni una cosa ni la otra, UNVERIFIED. En un corpus adversarial de SWE-bench, 21 de 21 veredictos
PROVEN fueron correctos, con 0 falsas finalizaciones; el método y sus límites están en
[docs/RESULTS.md](docs/RESULTS.md), y los mecanismos, en
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md).

### Un aprendizaje que solo conserva lo verificado

Tras cada ejecución, las trazas se convierten en una recompensa de proceso por nodo, y toda recompensa positiva
tiene que ir acompañada de evidencia del verificador. A partir de ahí, el bucle se ramifica: ventajas por carril de
las que aprende el router (GRPO), gradientes textuales que el paso de reflexión destila en lecciones, y una
comprobación de conflictos antes de escribir cualquier lección. Solo las lecciones verificadas llegan a un prompt, cada
prompt incluye el bloque aprendido y un registro de inyecciones anota exactamente qué recibió cada nodo,
de modo que el mérito vuelve a la lección que lo ganó. Hoy los gradientes y los temas verificados
sí llegan a los prompts, mientras que el enrutamiento aprendido todavía decide solo una pequeña minoría
de las rutas. Ciclo de vida y estado:
[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md).

### Automejora, con controles

mini-ork edita mini-ork a través del mismo grafo de recetas: `framework-edit` (autoediciones
verificadas con pruebas de especificación a ciegas), `self-improve` (escaneo, lentes, síntesis, implementación,
controles), `rsi-technique-review` (lee la literatura y propone técnicas que implementar) y
`goal-loop`. Un cambio aprendido debe superar una auditoría de hackeabilidad y una auditoría del verificador, y después
un control de no regresión sobre sondas congeladas frente a un grupo de control con reejecución a ciegas; además, cada versión
promovida conserva un puntero de reversión. El bucle de aplicación que reescribe prompts sin supervisión viene
**desactivado por defecto** (consulta la advertencia más abajo). `framework-edit` es la receta más ejecutada, y
`self-improve` encontró e integró su propia corrección el 2026-10-23. Detalles:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

## Comandos habituales

| Comando | Qué hace |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | Ejecuta una receta de principio a fin |
| `mini-ork doctor` | Comprueba dependencias, entorno y verificación previa de proveedores |
| `mini-ork validate` | Comprobaciones estáticas previas a la ejecución, con sugerencias `Fix:` |
| `mini-ork providers status <lane>` | Muestra si hay credenciales y de dónde vienen (nunca sus valores) |
| `mini-ork serve` | Interfaz web local y API en `127.0.0.1:7090` |
| `mini-ork garden` | Detección de deriva: colisiones, huérfanos, ejecuciones obsoletas |
| `mini-ork recover` / `resume` | Recupera una ejecución fallida o interrumpida |
| `mini-ork mcp` / `vault` | Registro de servidores MCP; cadena de backends del almacén de secretos |
| `mini-ork zed setup` / `cn status` | Integración con el editor; nivel y conectividad de ContextNest |

Recetas para empezar: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`. Consulta [recipes/](recipes/) y la
[guía de extensión](docs/EXTENSION.md).

## Actualización

Vuelve a ejecutar el instalador o, desde una copia local, ejecuta `make install`; puedes repetirlo sin riesgo:
reutiliza `.venv`, actualiza el paquete editable, repara el comando gestionado y verifica
las herramientas del sistema operativo. Después, `mini-ork update` aplica las migraciones de base de datos pendientes
e informa de la deriva de configuración.

## Desinstalación

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

El estado de cada proyecto vive en su propio `.mini-ork/`; bórralo allí donde quieras eliminarlo.
Tus copias del código fuente y tu historial de git nunca se tocan.

## Requisitos

| | |
|---|---|
| SO | macOS, Linux, WSL. Windows nativo: instala Python 3.11, Git, jq, yq y SQLite con `winget` y luego ejecuta `py -3 .\scripts\full_install.py` |
| Python | 3.11 o 3.12 |
| Git | obligatorio |
| Docker | solo para la verificación en sandbox (`certify`, Crucible) |
| Rust 1.80+ | solo para ContextNest y el crate del IDE |
| Acceso a modelos | un carril de proveedor configurado para ejecuciones reales; las ejecuciones en seco no necesitan ninguno |

## Más información

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | Por qué existe el proyecto; mediciones; lo que no pretende afirmar |
| [docs/SAFETY.md](docs/SAFETY.md) | Postura ante la automodificación y los controles que la sostienen |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Diseño del sistema y diagramas de componentes |
| [docs/operator](docs/operator) | Cómo operar mini-ork: variables de entorno, resolución de problemas |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | Integra mini-ork en tu propia aplicación |
| [docs/RESULTS.md](docs/RESULTS.md) | Métodos y tamaños de muestra detrás de las cifras |
| [ROADMAP.md](ROADMAP.md) | Hacia dónde va el proyecto |

> [!WARNING]
> mini-ork incluye un bucle de aplicación capaz de reescribir sus propios prompts y flujos de trabajo y de
> promover el cambio sin revisión humana. Viene **desactivado por defecto** (tanto
> `MO_APPLY_ENABLED` como `MO_AUTO_APPLY` deben valer `1`), y su radio de impacto, sus límites y sus
> mecanismos de reversión están documentados en [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended).

## Contribución y licencia

mini-ork se distribuye bajo **Apache-2.0**, © Amir Khakshour. Es un proyecto joven y de nivel de investigación: usa un
worktree dedicado para los cambios en el framework, acompaña cada afirmación sobre el comportamiento con un verificador y
ejecuta las comprobaciones específicas de la superficie que modifiques. Consulta [AGENTS.md](AGENTS.md) para el
flujo de contribución y [GOVERNANCE.md](GOVERNANCE.md) para la dirección del proyecto.

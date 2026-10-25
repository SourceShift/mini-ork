
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>Операционная система задач для ИИ-агентов — и она заставляет их доказывать свою работу.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | <a href="README.ko.md">한국어</a> | <a href="README.es.md">Español</a> | <a href="README.pt-BR.md">Português (Brasil)</a> | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | Русский | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.tr.md">Türkçe</a> | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="Запуск code-fix в IDE mini-ork: классификация, план, реализация, проверка, три линзы ревью, публикация, затем Keep или Reject для каждого изменённого файла" width="900">
  </a>
</p>
<p align="center"><sub>Запуск <code>code-fix</code> в IDE mini-ork: классификация → план → реализация → проверка → три линзы ревью на трёх семействах моделей → публикация, после чего каждый изменённый файл ждёт решения Keep или Reject. Пример запуска из <a href="https://miniork.dev/">пошагового обзора</a> · <a href="https://miniork.dev/media/mini-ork-demo.mp4">видео в полном качестве</a>.</sub></p>

mini-ork превращает цель в спланированный, выполненный и **проверенный** запуск на целом
парке моделей: **классификация → план → выполнение → проверка → рефлексия → улучшение**.
Каждый вердикт опирается на то, что код реально сделал при запуске — тесты, проверки типов,
схемы, выполнение в изолированной песочнице, — а не на мнение модели о собственном результате.

Эта страница — руководство по установке. Зачем нужен проект, объясняет документ
[Зачем существует mini-ork](docs/WHY.md).

## Быстрый старт

macOS, Linux или WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

Установщик:

- клонирует mini-ork в `~/.local/share/mini-ork` (путь меняется через `MINI_ORK_INSTALL_DIR`);
- создаёт `.venv` прямо в каталоге клона и ставит полный Python-профиль
  (CLI, локальный веб-sidecar, Crucible);
- добавляет `mini-ork` в PATH через `~/.local/bin`;
- предлагает мастер первого запуска, если работает в терминале.

Та же установка из уже существующего клона:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` подтягивает `ui/` — крейт IDE
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)). Python-рантайму
он не нужен; как собрать IDE, описано в [docs/UI.md](docs/UI.md).

## Первый запуск: `mini-ork onboard`

Одна команда превращает чистую машину в рабочее пространство, где уже можно запустить рецепт.
Она не обращается к моделям и не тратит деньги: каждый шаг — локальная проверка или локальная сборка.

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| Шаг | Что происходит |
|---|---|
| prerequisites | Проверяет Python ≥ 3.11 и git; отмечает `sqlite3` / `jq`, если они установлены |
| workspace | Создаёт `.mini-ork/` в текущем каталоге (при необходимости выполняет `git init`) |
| provider lanes | Сообщает, сохранены ли учётные данные, или называет команду, которая их добавит |
| contextnest | *Опционально:* клонирует и собирает локальный сервис памяти, создаёт для него начальный конфиг |
| zed | *Опционально:* прописывает агента mini-ork и MCP-серверы в `settings.json` редактора Zed |

Опциональные компоненты предлагаются, только если установлен их инструментарий (`cargo`, `zed`),
и каждый можно выбрать явно:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

Коды выхода: `0` — готово (пропущенные компоненты ошибкой не считаются), `1` — выбранный
компонент упал в режиме `--strict`, `2` — ошибка использования. В неинтерактивном режиме
вопросов нет: `-y` принимает все значения по умолчанию.

### ContextNest — локальный слой памяти

HTTP-сервис на Rust, который помнит, что прошлые сессии решили, где ошиблись, что проверили
и что выпустили. Мастер создаёт `config/contextnest.yaml` для **простого** уровня; с
`--secure` он ничего не записывает, а сервис не запустится, пока не задан `CN_TOKEN`.

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

Вручную: `git clone https://github.com/SourceShift/ContextNest.git`, затем
`make cn-serve` (слушает `127.0.0.1:28080`). Нужен Rust 1.80+. Подробности:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md).

### Zed — интеграция с редактором

Подключает к Zed `mini-ork acp` (внешний агент) и `mini-ork mcp-context` (контекст запусков,
обучения и затрат, только для чтения). Перед каждой записью в `settings.json` создаётся
резервная копия с меткой времени.

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

См. [docs/ZED.md](docs/ZED.md) и [docs/UI.md](docs/UI.md).

## Первый проверенный рабочий процесс

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

В kickoff-файле указываются целевой репозиторий, разрешённые файлы, ожидаемый артефакт и
команда или правило, которые доказывают успех. Артефакты запуска попадают в `.mini-ork/runs/`,
сохранённое состояние — в `.mini-ork/state.db`. Чтобы поменять, какой лейн обслуживает
какую роль, отредактируйте `.mini-ork/config/agents.yaml`.

### Докажите исправление одной командой

`mini-ork certify` берёт изменение и баг, который оно якобы исправляет, воспроизводит баг
на вашем коде, генерирует входные данные, под которые изменение не писалось, прогоняет всё
в Docker-песочнице и возвращает вердикт с приложенными доказательствами:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

Коды выхода: `0` PROVEN, `1` REFUTED, `2` UNVERIFIED — без уверенности команда воздерживается
от вердикта, а не гадает, поэтому её можно ставить гейтом в CI. Нужны Docker и один лейн модели.
Попробуйте `bash examples/certify-demo/demo.sh`.

## Верификация, обучение и самоулучшение

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="Карта зон на miniork.dev: по очереди подсвечиваются цикл запуска, цикл обучения, цикл починки и цикл RSI, и каждый показывает, что передаётся между движком, верификацией, обучением и восстановлением" width="900">
  </a>
</p>
<p align="center"><sub>Четыре цикла вокруг одного движка, из <a href="https://miniork.dev/">пошагового обзора</a> (демонстрационные данные) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">видео в полном качестве</a>.</sub></p>

GIF в начале страницы показывает цикл запуска. Вокруг него — ещё три слоя.

### Верификация, которую агенту не обойти словами

Каждый вердикт — это свидетельство выполнения. Верификаторы рецепта запускают защиту от
подмены, набор тестов, повторный прогон на базовом коде (тест обязан падать на старой версии)
и проверку адекватности через мутации. Второй лейн модели ещё до старта реализатора пишет по
kickoff-файлу слепые spec-тесты, и реализатор не может их править. Цепочка гейтов публикатора
отклоняет всё, что не прошло, а модель-ревьюер может наложить вето на результат, но никогда
не может засчитать прохождение. `certify` применяет тот же подход к отдельному изменению и
отвечает PROVEN или REFUTED, а если не может доказать ни то, ни другое — UNVERIFIED. На
состязательном корпусе SWE-bench верными оказались 21 из 21 вердикта PROVEN при 0 ложных
завершений; методика и её ограничения описаны в [docs/RESULTS.md](docs/RESULTS.md), механизмы — в
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md).

### Обучение, которое сохраняет только проверенное

После каждого запуска трассы превращаются в процессную награду для каждого узла, причём
положительная награда должна подкрепляться свидетельствами верификатора. Дальше цикл расходится
на несколько ветвей: преимущества по лейнам, на которых учится роутер (GRPO), текстовые градиенты,
из которых шаг рефлексии извлекает уроки, и проверка на конфликты перед записью любого урока.
В промпт попадают только проверенные уроки, каждый промпт несёт блок выученного, а журнал инъекций
фиксирует, что именно получил каждый узел, так что заслуга возвращается тому уроку, который её
принёс. Сегодня градиенты и проверенные темы действительно попадают в промпты, а выученная
маршрутизация пока определяет лишь небольшую долю маршрутов. Жизненный цикл и статус:
[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md).

### Самоулучшение под контролем гейтов

mini-ork правит mini-ork через тот же граф рецептов: `framework-edit` (проверенные самоправки
со слепыми spec-тестами), `self-improve` (сканирование, линзы, синтез, реализация, гейты),
`rsi-technique-review` (читает литературу и предлагает техники для реализации) и `goal-loop`.
Выученное изменение должно пройти аудит на взламываемость и аудит верификатора, затем гейт
нерегрессии на замороженных пробах против контрольной группы со слепым перезапуском, а у каждой
продвинутой версии сохраняется указатель для отката. Цикл применения, который переписывает
промпты без участия человека, поставляется **выключенным по умолчанию** (см. предупреждение ниже).
`framework-edit` — самый часто запускаемый рецепт, а `self-improve` 2026-10-23 сам нашёл и
влил собственное исправление. Подробности:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

## Повседневные команды

| Команда | Что делает |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | Прогоняет один рецепт от начала до конца |
| `mini-ork doctor` | Проверяет зависимости, окружение и preflight провайдеров |
| `mini-ork validate` | Статические проверки перед запуском с подсказками `Fix:` |
| `mini-ork providers status <lane>` | Показывает, есть ли учётные данные и откуда они (но не сами значения) |
| `mini-ork serve` | Локальный веб-интерфейс и API на `127.0.0.1:7090` |
| `mini-ork garden` | Обнаружение дрейфа: коллизии, осиротевшие объекты, устаревшие запуски |
| `mini-ork recover` / `resume` | Поднимает упавший или прерванный запуск |
| `mini-ork mcp` / `vault` | Реестр MCP-серверов; цепочка бэкендов хранилища секретов |
| `mini-ork zed setup` / `cn status` | Подключение редактора; уровень и доступность ContextNest |

Стартовые рецепты: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`. См. [recipes/](recipes/) и
[руководство по расширению](docs/EXTENSION.md).

## Обновление

Запустите установщик ещё раз или выполните `make install` в клоне — повторный запуск безопасен:
он переиспользует `.venv`, обновляет editable-пакет, чинит управляемую команду и проверяет
системные инструменты. Затем `mini-ork update` применяет ожидающие миграции базы данных и
сообщает о дрейфе конфигурации.

## Удаление

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

Состояние каждого проекта хранится в его `.mini-ork/`; удалите этот каталог там, где он больше
не нужен. Ваши рабочие копии исходников и история git никогда не затрагиваются.

## Требования

| | |
|---|---|
| ОС | macOS, Linux, WSL. Нативная Windows: установите Python 3.11, Git, jq, yq и SQLite через `winget`, затем выполните `py -3 .\scripts\full_install.py` |
| Python | 3.11 или 3.12 |
| Git | обязателен |
| Docker | только для верификации в песочнице (`certify`, Crucible) |
| Rust 1.80+ | только для ContextNest и крейта IDE |
| Доступ к моделям | один настроенный лейн провайдера для реальных запусков; для dry run не нужен ни один |

## Подробнее

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | Зачем существует проект; измерения; на что он не претендует |
| [docs/SAFETY.md](docs/SAFETY.md) | Подход к самомодификации и гейты, которые её сдерживают |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Архитектура системы и диаграммы компонентов |
| [docs/operator](docs/operator) | Эксплуатация mini-ork: переменные окружения, устранение неполадок |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | Встраивание mini-ork в собственное приложение |
| [docs/RESULTS.md](docs/RESULTS.md) | Методы и размеры выборок, на которых основаны цифры |
| [ROADMAP.md](ROADMAP.md) | Куда движется проект |

> [!WARNING]
> mini-ork содержит цикл применения, который может переписывать собственные промпты и рабочие
> процессы и продвигать изменения без проверки человеком. По умолчанию он **выключен** (обе
> переменные, `MO_APPLY_ENABLED` и `MO_AUTO_APPLY`, должны быть равны `1`), а его радиус
> поражения, лимиты и откаты описаны в [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended).

## Участие в разработке и лицензия

mini-ork распространяется под лицензией **Apache-2.0**, © Amir Khakshour. Проект на ранней стадии
и исследовательского уровня: вносите изменения во фреймворк в отдельном worktree, подкрепляйте
каждое утверждение о поведении верификатором и запускайте точечные проверки для той части,
которую меняете. Процесс внесения вклада описан в [AGENTS.md](AGENTS.md), направление развития
проекта — в [GOVERNANCE.md](GOVERNANCE.md).

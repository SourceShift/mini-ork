# Board poll without FastAPI — revision 7

## Goal

Profiling `board --json --shell` on the researcher home (4,763 runs) after
revision 6: `_runs` 2.52 s of which **2.1 s is importing FastAPI + pydantic**,
because `mini_ork/acp/history.py` (and every board/page module) imports `db_for`
from `mini_ork.web.deps`, whose module top does `from fastapi import Depends, …`.
The real work is ~0.3 s. `mini_ork.web.db` itself imports in 0.15 s with neither
fastapi nor pydantic loaded.

## Files in scope

- `mini_ork/web/db.py` — gains `db_for(home)` and its per-home cache
- `mini_ork/web/deps.py` — keeps `db_for` as a re-export (`from .db import db_for`), unchanged behaviour for the web server
- every module on the board/IDE path that does `from mini_ork.web.deps import db_for`:
  `mini_ork/acp/fleet.py`, `mini_ork/acp/history.py`, `mini_ork/acp/recipe_view.py`,
  `mini_ork/acp/commands.py`, `mini_ork/cli/board_cmd.py`, `mini_ork/ide_pages/*.py`
  → `from mini_ork.web.db import db_for`
- `tests/unit/test_board_cmd.py` (one new test)

Do not change `mini_ork/acp/agent.py` beyond that import, and nothing else.

## Change (exact)

1. Move `db_for` and `_dbs` / `_dbs_lock` from `web/deps.py` into `web/db.py`
   (same semantics: one `StateDB` per home, thread-safe). `web/deps.py` imports
   it from `.db` and keeps every other name it has today; `get_db` keeps working.
2. Switch the listed importers to `mini_ork.web.db.db_for`. Only modules whose
   other imports from `web.deps` are just `db_for`; if a module also needs a
   FastAPI dependency (e.g. `get_home`), leave that import as it is.

## Test

`tests/unit/test_board_cmd.py`: run, in a subprocess,
`import mini_ork.cli.board_cmd as b; b._runs(<temp home>); from mini_ork.ide_pages.header import header`
and assert `"fastapi" not in sys.modules and "pydantic" not in sys.modules`.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_*.py tests/unit/test_acp_task_state.py tests/test_web_smoke.py` passes — paste the summary line.
- ruff clean on touched files; the diff touches only files in scope.

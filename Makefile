# mini-ork — operator targets.
#
# install-hooks       Activate .githooks/ as the project's hooks dir.
# readme-claim-check  Run Layer 1 mechanical drift check (sub-second, free).
# readme-drift-panel  Run Layer 2b 4-lens drift audit (manual; ~$0.30 / 30-60s).
# uninstall-hooks     Reset hooks-path to git default.
#
# Observability UI targets:
# web-deps            Install Python + JS deps for the read-only obs UI.
# web-build           Build the React SPA into mini_ork/web/static/.
# web-serve           Boot the FastAPI sidecar at http://127.0.0.1:7090.
# web-dev             Boot Vite dev server (proxy → :7090) at http://localhost:7070.
# web-up              Boot API + Vite dev in parallel (Ctrl-C kills both).
# web-test            Run the observability smoke test suite.

.PHONY: install-hooks uninstall-hooks readme-claim-check readme-drift-panel help test \
        web-deps web-build web-serve web-dev web-up web-test dev-all \
        lint lint-advisory \
        worktree worktree-merge worktree-clean worktree-list

PORT ?= 7090

help:
	@echo "mini-ork operator targets:"
	@echo "  make install-hooks       activate .githooks/ (one-time setup per clone)"
	@echo "  make uninstall-hooks     reset to git default hooks dir"
	@echo "  make readme-claim-check  run mechanical README drift check (Layer 1)"
	@echo "  make readme-drift-panel  run 4-lens LLM drift audit (Layer 2b, ~\$$0.30)"
	@echo "  make lint                ruff blocking tier (F + E9) — must stay green"
	@echo "  make lint-advisory       ruff advisory tier (E,W,I,UP,B) — ratchet report"
	@echo "  make test                run the Python test suite"
	@echo ""
	@echo "Observability UI:"
	@echo "  make web-deps            install fastapi + uvicorn + pyyaml + pnpm install"
	@echo "  make web-build           build React SPA into mini_ork/web/static/"
	@echo "  make web-serve           boot FastAPI sidecar on PORT=\$$(PORT)"
	@echo "  make web-dev             boot Vite dev server on :7070 (proxy → :\$$(PORT))"
	@echo "  make web-up              boot API + Vite dev in parallel (Ctrl-C kills both)"
	@echo "  make dev-all             alias for web-up — all FE+BE with hot reload"
	@echo "  make web-test            run tests/test_web_smoke.py"
	@echo ""
	@echo "Worktree-first dev (keep main clean):"
	@echo "  make worktree SLUG=<slug>       create a task worktree + branch"
	@echo "  make worktree-merge [SLUG=<s>]  rebase origin/main, green-gate, push HEAD:main"	@echo "  make worktree-clean SLUG=<slug> remove worktree + delete branch"
	@echo "  make worktree-list              list all worktrees"

install-hooks:
	@git config core.hooksPath .githooks
	@chmod +x .githooks/pre-push 2>/dev/null || true
	@chmod +x scripts/readme-claim-check.sh \
	          scripts/readme-drift-gatekeeper.sh \
	          scripts/readme-drift-panel.sh 2>/dev/null || true
	@echo "✓ hooks installed: core.hooksPath = .githooks"
	@echo "  pre-push will run Layer 1 (mechanical) on every push,"
	@echo "  Layer 2a (gatekeeper) + 2b (panel) on push-to-main with structural diff."
	@echo "  Bypass with MO_README_DRIFT_SKIP=1 or --no-verify if needed."

uninstall-hooks:
	@git config --unset core.hooksPath 2>/dev/null || true
	@echo "✓ hooks dir reset to git default"

test:
	@python3 -m pytest -q

# ── python lint (ruff; tiers match the CI python-lint job) ──────────────────
# Blocking tier is [tool.ruff.lint] select in pyproject.toml (F + E9).
# Advisory tier is the non-blocking ratchet set (E,W,I,UP,B).
lint:
	@command -v ruff >/dev/null || { echo "ruff not found — pip install ruff"; exit 1; }
	ruff check mini_ork/ tests/

lint-advisory:
	@command -v ruff >/dev/null || { echo "ruff not found — pip install ruff"; exit 1; }
	-ruff check mini_ork/ tests/ --select E,W,I,UP,B --statistics

# ── worktree-first dev ───────────────────────────────────────────────────────
worktree:
	@[ -n "$(SLUG)" ] || { echo "usage: make worktree SLUG=<slug> [OWNS=\"path1 path2\"]"; exit 1; }
	@bash scripts/mini-ork-worktree.sh create "$(SLUG)" $(if $(OWNS),$(foreach p,$(OWNS),--owns $(p)),)

worktree-merge:
	@bash scripts/mini-ork-worktree.sh merge $(SLUG)

worktree-clean:
	@[ -n "$(SLUG)" ] || { echo "usage: make worktree-clean SLUG=<slug>"; exit 1; }
	@bash scripts/mini-ork-worktree.sh clean "$(SLUG)"

worktree-list:
	@bash scripts/mini-ork-worktree.sh list

readme-claim-check:
	@bash scripts/readme-claim-check.sh

readme-drift-panel:
	@bash scripts/readme-drift-panel.sh

# ── observability UI ─────────────────────────────────────────────────────────

web-deps:
	@echo "→ python deps"
	@pip install --quiet fastapi 'uvicorn[standard]' pyyaml || \
		{ echo "pip install failed — try: python3 -m pip install fastapi 'uvicorn[standard]' pyyaml"; exit 1; }
	@echo "→ js deps"
	@if [ -d ui ]; then \
		cd ui && (command -v pnpm >/dev/null && pnpm install || \
		           command -v npm  >/dev/null && npm install  || \
		           { echo "neither pnpm nor npm found"; exit 1; }); \
	fi
	@echo "✓ web-deps ready — run 'make web-up' or 'make web-serve'"

web-build:
	@if [ ! -d ui/node_modules ]; then \
		echo "node_modules missing — run 'make web-deps' first" >&2; exit 1; \
	fi
	@cd ui && (command -v pnpm >/dev/null && pnpm build || npm run build)
	@echo "✓ SPA bundle emitted to mini_ork/web/static/"

web-serve:
	@command -v python3 >/dev/null || { echo "python3 not on PATH"; exit 1; }
	@bash bin/mini-ork-serve --port $(PORT)

web-dev:
	@cd ui && (command -v pnpm >/dev/null && pnpm dev || npm run dev)

# Parallel: API + Vite dev. trap forwards Ctrl-C to both children.
# --reload is always-on here because this is a dev workflow target;
# editing a .py file auto-restarts the API so route additions appear
# without a manual restart (which would otherwise silently 404).
#
# Preflight: free both ports before boot. A wedged uvicorn from a prior
# session can hold :7090 (won't drain on SIGTERM under the shared-conn
# bug); a leftover Vite can hold :7070. We SIGTERM first, then SIGKILL
# anything still bound 0.5s later, so re-running `make web-up` is
# idempotent — no manual `pkill` dance.
UI_PORT ?= 7070
web-up:
	@echo "→ preflight: freeing :$(PORT) (api) + :$(UI_PORT) (ui) if held"
	@for p in $(PORT) $(UI_PORT); do \
	  pids=$$(lsof -ti tcp:$$p 2>/dev/null); \
	  if [ -n "$$pids" ]; then \
	    echo "  killing pid(s) on :$$p → $$pids"; \
	    echo "$$pids" | xargs kill 2>/dev/null || true; \
	  fi; \
	done
	@sleep 0.5
	@for p in $(PORT) $(UI_PORT); do \
	  pids=$$(lsof -ti tcp:$$p 2>/dev/null); \
	  if [ -n "$$pids" ]; then \
	    echo "  SIGTERM ignored on :$$p — escalating to SIGKILL → $$pids"; \
	    echo "$$pids" | xargs kill -9 2>/dev/null || true; \
	  fi; \
	done
	@echo "→ booting API on :$(PORT) (reload) + Vite dev on :$(UI_PORT) (Ctrl-C stops both)"
	@trap 'kill 0' INT TERM; \
	  ( bash bin/mini-ork-serve --port $(PORT) --reload 2>&1 | sed 's/^/[api] /' ) & \
	  ( cd ui && (command -v pnpm >/dev/null && pnpm dev || npm run dev) 2>&1 | sed 's/^/[ui]  /' ) & \
	  wait

# Everything needed for UI development, hot-reloading on both sides:
# FastAPI sidecar (uvicorn --reload) + Vite dev server (HMR).
dev-all: web-up

web-test:
	@python3 -m pytest tests/test_web_smoke.py tests/test_otel_export.py -v
	@bash tests/test_self_improve_outcome.sh
	@MINI_ORK_OBS_SMOKE_DRY=1 bash tests/test_obs_surface.sh
	@echo ""
	@echo "↑ for full LLM-using obs validation (~\$$0.05-\$$0.15):"
	@echo "  bash tests/test_obs_surface.sh"

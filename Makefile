# mini-ork — operator targets.
#
# install             Provision system tools, the project venv, Python extras, and the command.
# install-hooks       Activate .githooks/ as the project's hooks dir.
# readme-claim-check  Run Layer 1 mechanical drift check (sub-second, free).
# readme-drift-panel  Run Layer 2b 4-lens drift audit (manual; ~$0.30 / 30-60s).
# uninstall-hooks     Reset hooks-path to git default.
#
# Observability API targets:
# web-serve           Boot the FastAPI API at http://127.0.0.1:7090.
# web-test            Run the observability smoke test suite.

.PHONY: install install-system-deps install-hooks uninstall-hooks readme-claim-check readme-drift-panel help test \
        web-serve web-test \
        lint lint-advisory \
        worktree worktree-merge worktree-clean worktree-list

PORT ?= 7090
VENV ?= .venv
PYTHON ?= python3
INSTALL_SYSTEM_DEPS ?= 1
INSTALL_ARGS ?=

help:
	@echo "mini-ork operator targets:"
	@echo "  make install             install system tools + .venv + full Python runtime + command"
	@echo "  make install-system-deps install required OS commands through the available package manager"
	@echo "  make install-hooks       activate .githooks/ (one-time setup per clone)"
	@echo "  make uninstall-hooks     reset to git default hooks dir"
	@echo "  make readme-claim-check  run mechanical README drift check (Layer 1)"
	@echo "  make readme-drift-panel  run 4-lens LLM drift audit (Layer 2b, ~\$$0.30)"
	@echo "  make lint                ruff blocking tier (F + E9) — must stay green"
	@echo "  make lint-advisory       ruff advisory tier (E,W,I,UP,B) — ratchet report"
	@echo "  make test                run the Python test suite"
	@echo ""
	@echo "Observability API:"
	@echo "  make web-serve           boot FastAPI API on PORT=\$$(PORT)"
	@echo "  make web-test            run tests/test_web_smoke.py"
	@echo ""
	@echo "Worktree-first dev (keep main clean):"
	@echo "  make worktree SLUG=<slug>       create a task worktree + branch"
	@echo "  make worktree-merge [SLUG=<s>]  rebase origin/main, green-gate, push HEAD:main"	@echo "  make worktree-clean SLUG=<slug> remove worktree + delete branch"
	@echo "  make worktree-list              list all worktrees"

install-hooks:
	@git config core.hooksPath .githooks
	@chmod +x .githooks/pre-push 2>/dev/null || true
	@chmod +x scripts/readme_claim_check.py \
	          scripts/mini_ork_worktree.py \
	          scripts/readme-drift-gatekeeper.sh \
	          scripts/readme-drift-panel.sh 2>/dev/null || true
	@echo "✓ hooks installed: core.hooksPath = .githooks"
	@echo "  pre-push will run Layer 1 (mechanical) on every push,"
	@echo "  Layer 2a (gatekeeper) + 2b (panel) on push-to-main with structural diff."
	@echo "  Bypass with MO_README_DRIFT_SKIP=1 or --no-verify if needed."

uninstall-hooks:
	@git config --unset core.hooksPath 2>/dev/null || true
	@echo "✓ hooks dir reset to git default"

# TODO(bash-removal): scripts/install-system-deps.sh is still bash; port it to
# Python (or fold it into scripts/full_install.py) and retarget this recipe.
install-system-deps:
	@bash scripts/install-system-deps.sh

install:
	@if [ "$(INSTALL_SYSTEM_DEPS)" = "1" ]; then \
		$(MAKE) --no-print-directory install-system-deps; \
	else \
		echo "→ skipping system dependency provisioning (INSTALL_SYSTEM_DEPS=0)"; \
	fi
	@$(PYTHON) scripts/full_install.py --venv "$(VENV)" $(INSTALL_ARGS)

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
	@$(PYTHON) scripts/mini_ork_worktree.py create "$(SLUG)" $(if $(OWNS),$(foreach p,$(OWNS),--owns $(p)),)

worktree-merge:
	@$(PYTHON) scripts/mini_ork_worktree.py merge $(SLUG)

worktree-clean:
	@[ -n "$(SLUG)" ] || { echo "usage: make worktree-clean SLUG=<slug>"; exit 1; }
	@$(PYTHON) scripts/mini_ork_worktree.py clean "$(SLUG)"

worktree-list:
	@$(PYTHON) scripts/mini_ork_worktree.py list

readme-claim-check:
	@$(PYTHON) scripts/readme_claim_check.py

# TODO(bash-removal): the drift panel/gatekeeper stay bash for now (LLM panel
# tools; the ported pre-push hook still calls them). Port + retarget later.
readme-drift-panel:
	@bash scripts/readme-drift-panel.sh

# ── observability API ────────────────────────────────────────────────────────

web-serve:
	@command -v python3 >/dev/null || { echo "python3 not on PATH"; exit 1; }
	@$(PYTHON) bin/mini-ork serve --port $(PORT)

web-test:
	@python3 -m pytest tests/test_web_smoke.py tests/test_otel_export.py -v
	# NOTE(bash-removal): the legacy bash obs smoke layers
	# (tests/test_self_improve_outcome.sh, tests/test_obs_surface.sh) were
	# retired; the pytest modules above are the observability gate now.

# mini-ork — operator targets.
#
# install-hooks       Activate .githooks/ as the project's hooks dir.
# readme-claim-check  Run Layer 1 mechanical drift check (sub-second, free).
# readme-drift-panel  Run Layer 2b 4-lens drift audit (manual; ~$0.30 / 30-60s).
# uninstall-hooks     Reset hooks-path to git default.

.PHONY: install-hooks uninstall-hooks readme-claim-check readme-drift-panel help

help:
	@echo "mini-ork operator targets:"
	@echo "  make install-hooks       activate .githooks/ (one-time setup per clone)"
	@echo "  make uninstall-hooks     reset to git default hooks dir"
	@echo "  make readme-claim-check  run mechanical README drift check (Layer 1)"
	@echo "  make readme-drift-panel  run 4-lens LLM drift audit (Layer 2b, ~\$$0.30)"

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

readme-claim-check:
	@bash scripts/readme-claim-check.sh

readme-drift-panel:
	@bash scripts/readme-drift-panel.sh

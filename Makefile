.PHONY: check test init-check setup-hooks scrub-tree

check: test init-check

test:
	uv run pytest

init-check:
	brenn-scrub init --check

setup-hooks:
	git config core.hooksPath .githooks
	@rm -f .git/hooks/pre-commit
	@command -v brenn-scrub >/dev/null 2>&1 || echo "brenn-scrub not on PATH: cargo install --locked brenn-scrub"
	@command -v gitleaks >/dev/null 2>&1 || echo "gitleaks not on PATH: install the release brenn-scrub pins (brenn-scrub reports it)"
	@echo "setup-hooks: done."

scrub-tree:
	brenn-scrub tree

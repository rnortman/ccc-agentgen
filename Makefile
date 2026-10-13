.PHONY: check test init-check scrub-tree

check: test init-check

test:
	uv run pytest

init-check:
	brenn-scrub init --check

scrub-tree:
	brenn-scrub tree

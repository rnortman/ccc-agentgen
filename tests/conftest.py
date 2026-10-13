from __future__ import annotations

from pathlib import Path

import pytest

from agentgen.render import role_template_name

# `roles/` and `.md.j2` taken from the production namer, so the filesystem
# convention is written out once.
TEMPLATE_PREFIX, TEMPLATE_SUFFIX = role_template_name("\x00").split("\x00")

# A heading from `partials/ccc-spawning.md.j2`, present iff the partial is.
SPAWNING_PARTIAL_SENTINEL = "Spawning subagents through ccc"

# Roles that drive a workflow: each routes a `## Replies` table over the
# results of the roles in its own variant, and takes the user's directives.
DRIVERS = {"orchestrator", "triage-orchestrator", "burndown-orchestrator"}


@pytest.fixture(autouse=True)
def isolate_ccc_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "isolated-xdg"))


def role_template_ids(templates_dir: Path) -> list[str]:
    """Every role id with a template under `<templates_dir>/roles/`."""
    paths = sorted(templates_dir.glob(f"{TEMPLATE_PREFIX}*{TEMPLATE_SUFFIX}"))
    ids = [p.name.removesuffix(TEMPLATE_SUFFIX) for p in paths]
    assert ids, f"no role templates under {templates_dir}; the naming convention moved"
    return ids


class Root:
    """A temporary `--root` with templates/ and variants/."""

    def __init__(self, path: Path) -> None:
        self.path = path
        (path / "templates" / "roles").mkdir(parents=True)
        (path / "templates" / "partials").mkdir(parents=True)
        (path / "variants").mkdir()
        (path / "shell").mkdir()
        (path / "shell" / "ccc-env.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    def role(self, role_id: str, frontmatter: str, body: str) -> Path:
        path = self.path / "templates" / "roles" / f"{role_id}.md.j2"
        path.write_text(f"---\n{frontmatter.strip()}\n---\n{body}", encoding="utf-8")
        return path

    def partial(self, name: str, text: str) -> Path:
        path = self.path / "templates" / "partials" / name
        path.write_text(text, encoding="utf-8")
        return path

    def variant(self, name: str, toml: str) -> Path:
        path = self.path / "variants" / f"{name}.toml"
        path.write_text(toml, encoding="utf-8")
        return path

    @property
    def templates(self) -> Path:
        return self.path / "templates"


@pytest.fixture
def root(tmp_path: Path) -> Root:
    return Root(tmp_path / "root")

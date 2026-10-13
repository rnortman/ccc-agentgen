"""Install all rendered agent variants and compose ccc preferences."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import AgentGenError
from .build import render_checked
from .emit import MARKER_NAME, check_out_dir, write_all
from .model import Variant, load_variant

DEPLOY_MARKER_NAME = ".agentgen-deploy"


def _is_dir_without_following(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_file_without_following(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


@dataclass(frozen=True)
class DeployResult:
    variants: list[tuple[Variant, list[Path]]]
    config_count: int
    configs_dir: Path
    stale_variant_dirs: list[Path]
    switcher_path: Path


def ccc_config_dir() -> Path:
    """Return ccc's effective configuration directory."""
    value = os.environ.get("XDG_CONFIG_HOME")
    if value:
        if not value.startswith("/"):
            raise AgentGenError(f"XDG_CONFIG_HOME must be an absolute path, got '{value}'")
        base = Path(value)
    else:
        home = os.environ.get("HOME")
        if home:
            if not home.startswith("/"):
                raise AgentGenError(f"HOME must be an absolute path, got '{home}'")
            base = Path(home) / ".config"
        else:
            base = Path("/.config")
    return base / "ccc"


# A preference file is composed into an agents config, which ccc refuses if it
# carries a site key. CCC_SITE_KEYS mirrors SITE_KEYS in
# ccc-harness/src/config.rs; PREFS_KEYS is AGENTS_KEYS there minus agents_dir,
# which deploy writes itself and refuses in a preference file.
PREFS_KEYS = frozenset({"default_harness", "agents"})
CCC_SITE_KEYS = frozenset({"claude", "codex", "mqtt", "process", "launch"})
PREFS_RULE = "preference files hold only default_harness and [agents.*]"


def _check_preference_keys(path: Path, data: dict[str, object], config_dir: Path) -> None:
    for key in sorted(data):
        if key == "agents_dir":
            raise AgentGenError(f"{path}: preference files must not define top-level agents_dir")
        if key in PREFS_KEYS:
            continue
        if key in CCC_SITE_KEYS:
            raise AgentGenError(
                f"{path}: `{key}` is site config and belongs in {config_dir / 'config.toml'}; "
                f"{PREFS_RULE}"
            )
        raise AgentGenError(f"{path}: unknown top-level key `{key}`; {PREFS_RULE}")


def _read_preferences(prefs_dir: Path, config_dir: Path) -> list[tuple[Path, str]]:
    try:
        os.lstat(prefs_dir)
    except FileNotFoundError:
        paths = []
    else:
        if not prefs_dir.is_dir():
            raise AgentGenError(f"prefs path is not a directory: {prefs_dir}")
        paths = sorted(prefs_dir.glob("*.toml"))
    if not paths:
        print(
            f"warning: no preference files in {prefs_dir}; no configs composed.\n"
            f"If this deployment succeeds, any previously generated configs under "
            f"{config_dir / 'configs'} are removed; a shell still selecting one of them has "
            f"CCC_AGENTS_CONFIG naming a nonexistent file, and ccc refuses to run (exit 2) "
            f"until that shell runs `ccc-use off`, or preferences are restored and redeployed.\n"
            f"Your preference files, {config_dir / 'config.toml'} and "
            f"{config_dir / 'agents.toml'} are not touched.\n"
            f"Start with: mkdir -p {shlex.quote(str(prefs_dir))} && cp "
            f"{shlex.quote(str(config_dir / 'agents.toml'))} "
            f"{shlex.quote(str(prefs_dir / 'default.toml'))}\n"
            f"then delete its agents_dir line. If {config_dir / 'agents.toml'} does not exist, "
            f"write {prefs_dir / 'default.toml'} by hand instead. A preference file holds only "
            f"default_harness and [agents.*].",
            file=sys.stderr,
        )
        return []
    preferences: list[tuple[Path, str]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise AgentGenError(f"{path}: not valid UTF-8: {exc}") from None
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise AgentGenError(f"{path}: invalid TOML: {exc}") from None
        _check_preference_keys(path, data, config_dir)
        preferences.append((path, text))
    return preferences


def _check_configs_dir(configs_dir: Path) -> None:
    if configs_dir.is_symlink():
        raise AgentGenError(f"configs path is a symlink: {configs_dir}")
    if not configs_dir.exists():
        return
    if not _is_dir_without_following(configs_dir):
        raise AgentGenError(f"configs path is not a directory: {configs_dir}")
    entries = sorted(os.scandir(configs_dir), key=lambda entry: entry.name)
    if not entries:
        return
    marker = configs_dir / DEPLOY_MARKER_NAME
    if not _is_file_without_following(marker):
        raise AgentGenError(
            f"configs directory {configs_dir} is not empty and holds no {DEPLOY_MARKER_NAME} marker, "
            "so it is not a previous deployment; refusing to delete anything in it"
        )
    for entry in entries:
        if entry.name == DEPLOY_MARKER_NAME:
            continue
        if not entry.is_dir(follow_symlinks=False):
            raise AgentGenError(f"configs directory {configs_dir} holds unexpected entry: {entry.name}")
        for child in sorted(os.scandir(entry.path), key=lambda item: item.name):
            if not child.is_file(follow_symlinks=False) or not child.name.endswith(".toml"):
                raise AgentGenError(
                    f"configs directory {configs_dir} holds unexpected entry: {child.path}"
                )


def _remove_owned_configs(configs_dir: Path) -> None:
    if configs_dir.is_symlink():
        raise AgentGenError(f"configs path is a symlink: {configs_dir}")
    if configs_dir.exists():
        if not _is_dir_without_following(configs_dir):
            raise AgentGenError(f"configs path is not a directory: {configs_dir}")
        for variant_entry in sorted(os.scandir(configs_dir), key=lambda entry: entry.name):
            if variant_entry.name == DEPLOY_MARKER_NAME:
                continue
            if not variant_entry.is_dir(follow_symlinks=False):
                raise AgentGenError(f"configs directory entry changed type: {variant_entry.path}")
            for child_entry in sorted(os.scandir(variant_entry.path), key=lambda entry: entry.name):
                if not child_entry.is_file(follow_symlinks=False) or not child_entry.name.endswith(".toml"):
                    raise AgentGenError(f"configs directory entry changed type: {child_entry.path}")
                os.unlink(child_entry.path)
            if not variant_entry.is_dir(follow_symlinks=False):
                raise AgentGenError(f"configs directory entry changed type: {variant_entry.path}")
            os.rmdir(variant_entry.path)
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / DEPLOY_MARKER_NAME).write_text("agentgen deploy\n", encoding="utf-8")


def deploy(root: Path) -> DeployResult:
    """Validate a complete deployment, then install it."""
    config_dir = ccc_config_dir()
    root = root.resolve()
    templates_dir = root / "templates"
    variants_dir = root / "variants"
    if not templates_dir.is_dir():
        raise AgentGenError(f"--root {root} does not contain templates/")
    if not variants_dir.is_dir():
        raise AgentGenError(f"--root {root} does not contain variants/")
    switcher_path = root / "shell" / "ccc-env.sh"
    if not switcher_path.is_file():
        raise AgentGenError(f"--root {root} does not contain shell/ccc-env.sh")

    variant_paths = sorted(variants_dir.glob("*.toml"))
    if not variant_paths:
        raise AgentGenError(f"--root {root} contains no variants/*.toml files")
    initial = [(path, load_variant(path)) for path in variant_paths]
    names: dict[str, Path] = {}
    for path, variant in initial:
        if variant.name in names:
            raise AgentGenError(
                f"duplicate variant name {variant.name!r} in {names[variant.name]} and {path}"
            )
        names[variant.name] = path

    rendered = [render_checked(path, root, variant=variant) for path, variant in initial]

    prefs_dir = config_dir / "prefs"
    preferences = _read_preferences(prefs_dir, config_dir)
    agents_dir = config_dir / "agents"
    for variant, _ in rendered:
        check_out_dir(agents_dir / variant.name)
    configs_dir = config_dir / "configs"
    _check_configs_dir(configs_dir)

    current_names = {variant.name for variant, _ in rendered}
    stale = sorted(
        path
        for path in agents_dir.iterdir()
        if path.is_dir()
        and (path / MARKER_NAME).is_file()
        and path.name not in current_names
    ) if agents_dir.is_dir() else []

    deployed: list[tuple[Variant, list[Path]]] = []
    for variant, roles in rendered:
        paths = write_all(agents_dir / variant.name, roles, variant.name)
        deployed.append((variant, paths))

    _remove_owned_configs(configs_dir)
    config_count = 0
    for variant, _ in rendered:
        for pref_path, text in preferences:
            variant_dir = configs_dir / variant.name
            variant_dir.mkdir(parents=True, exist_ok=True)
            config_path = variant_dir / pref_path.name
            content = (
                f"# agents config generated by agentgen deploy from {pref_path}; edit that file, "
                f"not this one; selected through CCC_AGENTS_CONFIG by ccc-use\n"
                f"agents_dir = {json.dumps(str(agents_dir / variant.name), ensure_ascii=False)}\n\n"
                f"{text}"
            )
            config_path.write_text(content, encoding="utf-8")
            config_count += 1

    return DeployResult(
        variants=deployed,
        config_count=config_count,
        configs_dir=configs_dir,
        stale_variant_dirs=stale,
        switcher_path=switcher_path,
    )

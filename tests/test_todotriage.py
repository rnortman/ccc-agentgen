from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import PROVENANCE_LINE_RE
from todotriage import TriageError, ops, todomd
from todotriage.cli import main
from todotriage.store import HEADER, BlockerState, Row, Status, Store, rows

TODO = """\
# TODOs

## `alpha`

Fix `src/core/engine.py` when the queue drains.

Code site (`TODO(alpha)`): src/core/engine.py.

## beta

Unbackticked entry touching src/core/engine.py.

## `gamma` [blocked-on: alpha]

Blocked on alpha. triaged 2026-06-10, re-surfaced later.

## `example-placeholder` (DO NOT TRIAGE — this is a fake entry)

Placeholder.
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "TODO.md").write_text(TODO, encoding="utf-8")
    (path / "src" / "core").mkdir(parents=True)
    (path / "src" / "core" / "engine.py").write_text(
        "x = 1  # TODO(alpha)\ny = 2  # TODO(beta) and TODO(orphan)\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init", "--date=2026-01-02T00:00:00")
    return path


@pytest.fixture
def store(repo: Path, tmp_path: Path) -> Store:
    return Store.init(tmp_path / "store", "core", "TODO.md", 180, ["example-placeholder"])


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def opts(repo: Path, store: Store) -> list[str]:
    return ["--repo", str(repo), "--store", str(store.root)]


def ruled(disposition: str = "do", *, date: dt.date | None = None, entry_hash: str = "",
          review: str | None = None, **extra: Any) -> dict[str, Any]:
    ruling = {"disposition": disposition, "priority": 2 if disposition == "do" else None,
              "summary": "s", "ruled_by": "agent",
              "date": (date or dt.date.today()).isoformat(), **extra}
    record: dict[str, Any] = {"pass": 1, "started": "t", "docs": [], "ruling": ruling}
    if review is not None:
        record["review"] = {"state": review, "date": dt.date.today().isoformat()}
    return {"passes": [record], "entry_hash": entry_hash}


def approved(disposition: str = "do", **kwargs: Any) -> dict[str, Any]:
    return ruled(disposition, review="approved", **kwargs)


def hash_of(repo: Path, slug: str) -> str:
    return todomd.read(repo, "TODO.md").by_slug()[slug].body_hash




def test_heading_forms() -> None:
    todo = todomd.parse(TODO)
    assert todo.title == "TODOs"
    assert [e.slug for e in todo.entries] == ["alpha", "beta", "gamma", "example-placeholder"]
    alpha, beta, gamma, placeholder = todo.entries
    assert alpha.backticked and not alpha.trail
    assert not beta.backticked
    assert gamma.trail.strip() == "[blocked-on: alpha]"
    assert placeholder.trail.strip().startswith("(DO NOT TRIAGE")
    assert alpha.line == 3 and alpha.end_line == 7
    assert alpha.body.startswith("\nFix")
    assert not [i for i in todo.issues if i.level == "error"]


def test_malformed_heading_is_an_error() -> None:
    todo = todomd.parse("# T\n\n## Not A Slug\n\nbody\n")
    assert todo.entries == []
    assert [(i.level, i.line) for i in todo.issues] == [("error", 3)]


def test_missing_title_duplicate_and_empty_body_are_errors() -> None:
    todo = todomd.parse("intro\n\n## `a`\n\nx\n\n## `a`\n\n## `b`\n\ny\n")
    messages = [(i.level, i.line, i.message) for i in todo.issues]
    assert ("error", 1, "file does not start with a `# ` title") in messages
    assert any(m[0] == "error" and m[1] == 7 and "duplicate slug `a`" in m[2] for m in messages)
    assert any(m[0] == "error" and m[1] == 7 and "empty body" in m[2] for m in messages)
    assert todo.by_slug()["a"].line == 3


def test_in_band_markers_are_warned_with_their_line() -> None:
    todo = todomd.parse(TODO)
    markers = [i for i in todo.issues if "in-band" in i.message]
    assert [(i.line, i.message.split("'")[1]) for i in markers] == [(15, "triaged 20")]


def test_body_hash_ignores_whitespace_changes() -> None:
    assert todomd.body_hash("\nfoo  bar\n\nbaz \n") == todomd.body_hash("foo bar baz")
    assert todomd.body_hash("foo bar") != todomd.body_hash("foo baz")


def test_first_seen_uses_git_history(repo: Path) -> None:
    assert todomd.first_seen(repo, "TODO.md", "## `alpha`") == "2026-01-02"
    assert todomd.first_seen(repo, "TODO.md", "## `never`") is None


def test_code_sites_exclude_the_todo_file(repo: Path) -> None:
    sites = todomd.code_sites(repo, "TODO.md")
    assert [(s.path, s.line, s.slug) for s in sites] == [
        ("src/core/engine.py", 1, "alpha"),
        ("src/core/engine.py", 2, "beta"),
        ("src/core/engine.py", 2, "orphan"),
    ]




def test_init_writes_header_and_refuses_to_overwrite(store: Store) -> None:
    text = (store.root / "store.yaml").read_text(encoding="utf-8")
    assert text.startswith(HEADER)
    assert store.config.tag == "core" and store.config.ignore == ("example-placeholder",)
    assert (store.root / "entries").is_dir() and (store.root / "sessions").is_dir()
    with pytest.raises(TriageError, match="already exists"):
        Store.init(store.root, "core", "TODO.md", 180, [])


def test_triage_yaml_round_trips_with_header(store: Store) -> None:
    data = ruled(blocked_on=["x"], cluster=["a", "b"])
    store.save("sessions", data)
    path = store.triage_path("sessions")
    assert path == store.root / "entries" / "sessions" / "triage.yaml"
    assert path.read_text(encoding="utf-8").startswith(HEADER)
    assert store.load("sessions") == data
    assert store.slugs() == ["sessions"]


def test_open_store_errors(tmp_path: Path) -> None:
    with pytest.raises(TriageError, match="todo-triage init"):
        Store.open(tmp_path)


@pytest.mark.parametrize(("config", "message"), [
    ("schema: 2\ntag: core\n", "unsupported schema 2"),
    ("tag: core\n", "unsupported schema None"),
    ("schema: 1\n", "malformed config"),
    ("schema: 1\ntag: core\nstale_days: soon\n", "malformed config"),
    ("- a list\n", "expected a mapping"),
])
def test_open_store_rejects_bad_config(tmp_path: Path, config: str, message: str) -> None:
    (tmp_path / "store.yaml").write_text(config, encoding="utf-8")
    with pytest.raises(TriageError, match=message):
        Store.open(tmp_path)


def test_init_validates_tag_and_stale_days(tmp_path: Path) -> None:
    with pytest.raises(TriageError, match="--tag must be non-empty"):
        Store.init(tmp_path, " ", "TODO.md", 180, [])
    with pytest.raises(TriageError, match="--stale-days must be at least 1"):
        Store.init(tmp_path, "core", "TODO.md", 0, [])
    assert not (tmp_path / "store.yaml").exists()


def statuses(store: Store, repo: Path, on: dt.date | None = None) -> dict[str, tuple[str, str]]:
    return {r.slug: (r.status, r.reason)
            for r in rows(store, todomd.read(repo, "TODO.md"), on)}


def test_derived_statuses(repo: Path, store: Store) -> None:
    assert statuses(store, repo) == {
        "alpha": ("untriaged", ""), "beta": ("untriaged", ""), "gamma": ("untriaged", ""),
        "example-placeholder": ("ignored", ""),
    }
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    store.save("beta", ruled(entry_hash="0" * 64))
    store.save("gamma", {"passes": [{"pass": 1, "started": "t", "docs": []}]})
    store.save("gone", ruled())
    got = statuses(store, repo)
    assert got["alpha"] == ("triaged", "")
    assert got["beta"][0] == "modified"
    assert got["gamma"][0] == "open"
    assert got["gone"][0] == "resolved"
    later = dt.date.today() + dt.timedelta(days=181)
    assert statuses(store, repo, later)["alpha"][0] == "stale"


def test_blocked_on_a_deleted_or_resolved_slug_is_stale(repo: Path, store: Store) -> None:
    store.save("gamma", ruled("blocked", blocked_on=["alpha", "some capability"],
                              entry_hash=hash_of(repo, "gamma")))
    assert statuses(store, repo)["gamma"][0] == "triaged"
    store.save("alpha", ruled("delete", entry_hash=hash_of(repo, "alpha")))
    assert statuses(store, repo)["gamma"] == ("triaged", "")
    store.save("alpha", approved("delete", entry_hash=hash_of(repo, "alpha")))
    assert statuses(store, repo)["gamma"] == ("stale", "blocked on `alpha`, which is ruled delete")
    store.save("gamma", ruled("blocked", blocked_on=["gone"], entry_hash=hash_of(repo, "gamma")))
    store.save("gone", ruled())
    assert statuses(store, repo)["gamma"] == ("stale", "blocked on `gone`, which is resolved")


def test_blocked_on_a_user_ruled_delete_sent_back_is_triaged(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gamma", ruled("blocked", blocked_on=["alpha"], priority=1,
                              entry_hash=hash_of(repo, "gamma")))
    store.save("alpha", user_ruled(disposition="delete", entry_hash=hash_of(repo, "alpha")))
    _, out, _ = run(capsys, "list", "--status", "stale", *opts(repo, store))
    assert statuses(store, repo)["gamma"] == ("stale", "blocked on `alpha`, which is ruled delete")
    assert [line.split("\t")[0] for line in out.splitlines()] == ["gamma"]
    code, _, err = run(capsys, "retriage", "alpha", "--note", "Keep it.", *opts(repo, store))
    assert code == 0, err
    assert statuses(store, repo)["gamma"] == ("triaged", "")




def test_lint_reports_warnings_and_orphans(repo: Path, store: Store,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "lint", *opts(repo, store))
    assert code == 0
    assert "warning: TODO.md:9: `beta`: slug is not backticked" in out
    assert "`gamma` has no `TODO(gamma)` code site" in out
    assert "example-placeholder` has no" not in out
    assert "src/core/engine.py:2: `TODO(orphan)` matches no entry" in out
    assert out.rstrip().endswith("0 error(s), 6 warning(s)")


def test_lint_without_store_and_exit_1_on_error(repo: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "lint", "--repo", str(repo))
    assert code == 0 and "example-placeholder` has no" in out
    (repo / "TODO.md").write_text(TODO + "\n## `alpha`\n\ndup\n", encoding="utf-8")
    code, out, _ = run(capsys, "lint", "--repo", str(repo), "--json")
    assert code == 1
    assert [e["line"] for e in json.loads(out)["errors"]] == [21]


def test_repo_must_be_a_git_work_tree(tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "lint", "--repo", str(tmp_path))
    assert code == 1 and "not a git work tree" in err


def test_store_required(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "list", "--repo", str(repo))
    assert code == 1 and "--store is required" in err


def test_init_command(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "new-store"
    code, out, _ = run(capsys, "init", "--repo", str(repo), "--store", str(target),
                       "--tag", "t", "--ignore", "example-placeholder")
    assert code == 0 and out.strip() == str(target / "store.yaml")
    assert Store.open(target).config.ignore == ("example-placeholder",)


def test_next_order_and_exit_3(repo: Path, store: Store,
                               capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "next", *opts(repo, store), "--json")
    assert code == 0 and json.loads(out) == {"slug": "alpha", "status": "untriaged", "pass": 1,
                                             "review": "-"}
    old = dt.date.today() - dt.timedelta(days=400)
    older = dt.date.today() - dt.timedelta(days=500)
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha"), date=old))
    store.save("gamma", ruled(entry_hash=hash_of(repo, "gamma"), date=older))
    store.save("beta", ruled(entry_hash="0" * 64))
    code, out, _ = run(capsys, "next", *opts(repo, store))
    assert out.splitlines() == ["slug: beta", "status: modified", "pass: 2", "review: pending"]
    code, out, _ = run(capsys, "next", *opts(repo, store), "--exclude", "beta")
    assert out.splitlines()[0] == "slug: gamma"
    code, out, _ = run(capsys, "next", *opts(repo, store), "--exclude", "beta", "gamma")
    assert out.splitlines()[0] == "slug: alpha"
    store.save("alpha", {**ruled(), "passes": ruled()["passes"] + [{"pass": 2, "docs": []}]})
    code, out, _ = run(capsys, "next", *opts(repo, store), "--exclude", "beta", "gamma")
    assert code == 3 and out.strip() == "nothing to triage"


def test_list_filters(repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha"), cluster=["alpha", "beta"]))
    store.save("beta", ruled("defer", defer_until="x", entry_hash=hash_of(repo, "beta")))
    code, out, _ = run(capsys, "list", *opts(repo, store))
    lines = out.splitlines()
    assert lines[0].split("\t") == ["alpha", "triaged", "pending", "do", "P2",
                                    dt.date.today().isoformat(), "alpha,beta"]
    assert len(lines) == 4
    _, out, _ = run(capsys, "list", *opts(repo, store), "--disposition", "defer")
    assert [line.split("\t")[0] for line in out.splitlines()] == ["beta"]
    _, out, _ = run(capsys, "list", *opts(repo, store), "--priority", "2")
    assert [line.split("\t")[0] for line in out.splitlines()] == ["alpha"]
    _, out, _ = run(capsys, "list", *opts(repo, store), "--status", "untriaged", "ignored")
    assert [line.split("\t")[0] for line in out.splitlines()] == ["gamma", "example-placeholder"]
    _, out, _ = run(capsys, "list", *opts(repo, store), "--stale")
    assert out == ""


def test_show_and_sites(repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    data = ruled(entry_hash=hash_of(repo, "alpha"))
    data["passes"][0]["docs"] = [
        {"step": "sketch", "path": "entries/alpha/sketch-t1.md", "date": "2026-09-29"},
        {"step": "ruling", "path": "entries/alpha/ruling-t1-a1.md", "date": "2026-09-29",
         "rejected": "priority missing"},
    ]
    store.save("alpha", data)
    code, out, _ = run(capsys, "show", "alpha", *opts(repo, store))
    assert code == 0
    assert "first seen: 2026-01-02" in out
    assert "## `alpha`\n\nFix" in out
    assert "  src/core/engine.py:1: x = 1  # TODO(alpha)" in out
    assert f"t1 ruling: {store.root}/entries/alpha/ruling-t1-a1.md (rejected: priority missing)" in out
    assert HEADER.splitlines()[0] in out
    code, _, err = run(capsys, "show", "nope", *opts(repo, store))
    assert code == 1 and "`nope` is neither in TODO.md nor in the store" in err
    code, out, _ = run(capsys, "sites", "beta", *opts(repo, store))
    assert out.strip() == "src/core/engine.py:2: y = 2  # TODO(beta) and TODO(orphan)"


def test_show_names_the_standing_ruling_document(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    def doc(step: str, name: str, **extra: str) -> dict[str, str]:
        return {"step": step, "path": f"entries/alpha/{name}", "date": "2026-09-29", **extra}

    data = approved(entry_hash=hash_of(repo, "alpha"))
    data["passes"][0]["docs"] = [doc("ruling", "ruling-t1-a1.md")]
    data["passes"].append({"pass": 2, "started": "t", "docs": [
        doc("sketch", "sketch-t2.md"), doc("ruling", "ruling-t2-a1.md", rejected="bad"),
        doc("ruling", "ruling-t2-a2.md")], "ruling": ruled("delete")["passes"][0]["ruling"]})
    store.save("alpha", data)
    _, out, _ = run(capsys, "show", "alpha", *opts(repo, store))
    assert f"ruling document: t2 {store.root}/entries/alpha/ruling-t2-a2.md" in out.splitlines()
    _, out, _ = run(capsys, "show", "alpha", "--json", *opts(repo, store))
    assert json.loads(out)["ruling_document"] == f"{store.root}/entries/alpha/ruling-t2-a2.md"

    data["passes"].append({"pass": 3, "started": "t", "docs": [doc("sketch", "sketch-t3.md")]})
    store.save("alpha", data)
    code, _, err = run(capsys, "override", "alpha", "--disposition", "delete",
                       *opts(repo, store))
    assert code == 0, err
    _, out, _ = run(capsys, "show", "alpha", *opts(repo, store))
    assert "ruling document: (ruled by the user)" in out.splitlines()
    assert "  t3 sketch: " in out
    _, out, _ = run(capsys, "show", "alpha", "--json", *opts(repo, store))
    assert json.loads(out)["ruling_document"] is None
    _, out, _ = run(capsys, "show", "beta", *opts(repo, store))
    assert "ruling document: (none)" in out.splitlines()


def test_related_ranks_clusters_dirs_and_paths(repo: Path, store: Store,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gamma", ruled(cluster=["gamma", "alpha"], entry_hash=hash_of(repo, "gamma")))
    code, out, _ = run(capsys, "related", "alpha", *opts(repo, store), "--json")
    got = {r["slug"]: r for r in json.loads(out)}
    assert list(got) == ["gamma", "beta"]
    assert got["gamma"]["reasons"] == ["prior cluster"]
    assert got["beta"]["reasons"] == ["site dirs: src/core", "paths: src/core/engine.py"]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def lines_of(out: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in out.splitlines())


def rows_by_slug(out: str) -> dict[str, list[str]]:
    return {line.split("\t")[0]: line.split("\t") for line in out.splitlines()}


def test_begin_prints_targets_and_refuses_a_second_open_pass(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "begin", "alpha", *opts(repo, store))
    d = store.entry_dir("alpha")
    assert code == 0 and lines_of(out) == {
        "slug": "alpha", "pass": "1", "exploration": f"{d}/exploration-t1-<N>.md",
        "sketch": f"{d}/sketch-t1.md", "contrarian": f"{d}/contrarian-t1.md",
        "ruling": f"{d}/ruling-t1-a1.md", "prior": "(none)", "abandoned": "(none)"}
    assert d.is_dir() and store.load("alpha")["passes"][0]["pass"] == 1
    code, _, err = run(capsys, "begin", "alpha", *opts(repo, store))
    assert code == 1 and "--resume" in err
    code, out, _ = run(capsys, "begin", "alpha", "--resume", *opts(repo, store))
    assert code == 0 and lines_of(out)["pass"] == "1"
    code, _, err = run(capsys, "begin", "example-placeholder", *opts(repo, store))
    assert code == 1 and "`example-placeholder` is ignored in store.yaml" in err
    code, _, err = run(capsys, "override", "example-placeholder", "--disposition", "delete",
                       *opts(repo, store))
    assert code == 1 and "is ignored" in err
    code, _, err = run(capsys, "begin", "nope", *opts(repo, store))
    assert code == 1 and "`nope` is not an entry in TODO.md" in err
    code, _, err = run(capsys, "begin", "beta", "--resume", *opts(repo, store))
    assert code == 1 and "`beta` has no open pass to resume" in err


def test_begin_on_a_retriage_lists_the_prior_pass(repo: Path, store: Store,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    data = ruled(entry_hash="0" * 64)
    data["passes"][0]["docs"] = [
        {"step": "ruling", "path": "entries/beta/ruling-t1-a1.md", "rejected": "x"},
        {"step": "ruling", "path": "entries/beta/ruling-t1-a2.md"}]
    store.save("beta", data)
    _, out, _ = run(capsys, "begin", "beta", *opts(repo, store))
    assert lines_of(out)["pass"] == "2"
    assert f"prior ruling: {store.root}/entries/beta/ruling-t1-a2.md" in out.splitlines()
    assert "ruling-t1-a1" not in out


def test_path_ordinals_and_cap(repo: Path, store: Store,
                               capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "path", "alpha", "--step", "ruling", *opts(repo, store))
    assert code == 1 and "`alpha` has no open pass" in err
    run(capsys, "begin", "alpha", *opts(repo, store))
    d = store.entry_dir("alpha")
    code, _, err = run(capsys, "path", "alpha", "--step", "notes", *opts(repo, store))
    assert code == 1 and "no ruling attempt" in err
    for a in (1, 2, 3):
        _, out, _ = run(capsys, "path", "alpha", "--step", "ruling", *opts(repo, store))
        assert out.strip() == f"{d}/ruling-t1-a{a}.md"
        write(Path(out.strip()), "escalated\n")
        _, out, _ = run(capsys, "path", "alpha", "--step", "notes", *opts(repo, store))
        assert out.strip() == f"{d}/notes-user-t1-a{a}.md"
    write(d / "notes-user-t1-a3.md", "words\n")
    code, _, err = run(capsys, "path", "alpha", "--step", "notes", *opts(repo, store))
    assert code == 1 and "notes-user-t1-a3.md already exists" in err
    code, _, err = run(capsys, "path", "alpha", "--step", "ruling", *opts(repo, store))
    assert code == 1 and "all 3 ruling attempts used" in err
    _, out, _ = run(capsys, "begin", "alpha", "--resume", *opts(repo, store))
    assert lines_of(out)["ruling"] == "none (all ruling attempts used)"


@pytest.fixture
def frozen_stamp(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin `ops.file_stamp` so two session paths in one test share a stamp."""
    stamp = "20260929-120000"
    monkeypatch.setattr(ops, "file_stamp", lambda when=None: stamp)
    return stamp


def test_path_selection_stamps_and_suffixes(repo: Path, store: Store, frozen_stamp: str,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(capsys, "path", "--selection", *opts(repo, store))
    first = Path(out.strip())
    assert first == store.root / "sessions" / f"selection-{frozen_stamp}.md"
    write(first, "x")
    _, out, _ = run(capsys, "path", "--selection", *opts(repo, store))
    assert Path(out.strip()) == first.with_name(f"selection-{frozen_stamp}-2.md")
    assert run(capsys, "path", "alpha", "--selection", *opts(repo, store))[0] == 1


def test_path_prune(repo: Path, store: Store, frozen_stamp: str,
                    capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(capsys, "path", "--prune", *opts(repo, store))
    first = Path(out.strip())
    assert first == store.root / "sessions" / f"prune-{frozen_stamp}.md"
    write(first, "x")
    _, out, _ = run(capsys, "path", "--prune", *opts(repo, store))
    assert Path(out.strip()) == first.with_name(f"prune-{frozen_stamp}-2.md")
    code, _, err = run(capsys, "path", "--prune", "--selection", *opts(repo, store))
    assert code == 1 and "give one of --selection or --prune" in err
    code, _, err = run(capsys, "path", "alpha", "--prune", *opts(repo, store))
    assert code == 1 and "--prune takes no SLUG or --step" in err


def sketch(store: Store, slug: str, covers: list[str], t: int = 1) -> Path:
    return write(store.entry_dir(slug) / f"sketch-t{t}.md",
                 f"---\ncovers: [{', '.join(covers)}]\n---\n\n# Sketch\n")


def test_log_scan_and_cluster_mirroring(repo: Path, store: Store,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    d = store.entry_dir("alpha")
    for n in (2, 1, 10):
        write(d / f"exploration-t1-{n}.md", "report\n")
    write(d / "exploration-t2-1.md", "other pass\n")
    write(d / "review-exploration-1.md", "triage-clerk report, not triage evidence\n")
    code, out, _ = run(capsys, "log", "alpha", "--step", "exploration", "--scan",
                       *opts(repo, store))
    assert code == 0
    docs = store.load("alpha")["passes"][0]["docs"]
    assert [x["path"] for x in docs] == [f"entries/alpha/exploration-t1-{n}.md" for n in (1, 2, 10)]
    _, out, _ = run(capsys, "log", "alpha", "--step", "exploration", "--scan", *opts(repo, store))
    assert out.strip() == "no unregistered exploration documents"

    store.save("beta", ruled(entry_hash=hash_of(repo, "beta")))
    path = sketch(store, "alpha", ["alpha", "beta"])
    code, out, _ = run(capsys, "log", "alpha", "--step", "sketch", "--doc", str(path),
                       *opts(repo, store))
    assert code == 0 and out.splitlines()[-2].endswith("-> alpha, beta")
    assert out.splitlines()[-1] == "held: (none)"
    beta = store.load("beta")
    assert [p["pass"] for p in beta["passes"]] == [1, 2]
    assert beta["passes"][1]["docs"][0]["covers"] == ["alpha", "beta"]
    assert (store.triage_path("beta").read_text(encoding="utf-8")).startswith(HEADER)

    bad = sketch(store, "alpha", ["beta"], t=9)
    code, _, err = run(capsys, "log", "alpha", "--step", "sketch", "--doc", str(bad),
                       *opts(repo, store))
    assert code == 1 and "does not include `alpha`" in err
    outside = write(repo / "sketch.md", "x\n")
    code, _, err = run(capsys, "log", "alpha", "--step", "sketch", "--doc", str(outside),
                       *opts(repo, store))
    assert code == 1 and "is not inside the store" in err
    code, _, err = run(capsys, "log", "alpha", "--step", "sketch", *opts(repo, store))
    assert code == 1 and "give exactly one of --doc PATH or --scan" in err
    code, _, err = run(capsys, "log", "alpha", "--step", "sketch", "--scan", *opts(repo, store))
    assert code == 1 and "--scan applies only to --step exploration" in err


def ruling_doc(store: Store, slug: str, a: int, frontmatter: str) -> Path:
    return write(store.entry_dir(slug) / f"ruling-t1-a{a}.md",
                 f"---\n{frontmatter}---\n\n## Decision\n")


def test_record_rejects_then_records_a_cluster(repo: Path, store: Store,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "log", "alpha", "--step", "sketch", "--doc",
        str(sketch(store, "alpha", ["alpha", "beta"])), *opts(repo, store))
    bad = ruling_doc(store, "alpha", 1, "rulings:\n  alpha:\n    disposition: do\n"
                                        "    summary: s\n")
    code, out, err = run(capsys, "record", "alpha", "--ruling", str(bad), *opts(repo, store))
    assert code == 1 and out == ""
    assert err.splitlines() == [
        f"{bad}:2: `beta` is covered by the sketch but has no ruling",
        f"{bad}:3: `alpha`: priority must be 1-4 for `do`, got None",
    ]
    rejected = store.load("alpha")["passes"][0]["docs"][-1]
    assert rejected["path"] == "entries/alpha/ruling-t1-a1.md"
    assert rejected["rejected"].startswith("`beta` is covered by the sketch")
    assert store.load("alpha")["passes"][0].get("ruling") is None

    good = ruling_doc(store, "alpha", 2, (
        "rulings:\n  alpha:\n    disposition: blocked\n    blocked_on: [beta]\n"
        "    summary: after beta\n  beta:\n    disposition: do\n    priority: 2\n"
        "    summary: do it\n"))
    code, out, _ = run(capsys, "record", "alpha", "--ruling", str(good), *opts(repo, store))
    assert code == 0 and out.strip() == "recorded: alpha, beta"
    alpha, beta = store.load("alpha"), store.load("beta")
    assert alpha["passes"][0]["ruling"] == {
        "disposition": "blocked", "priority": None, "blocked_on": ["beta"],
        "defer_until": None, "summary": "after beta", "blocked_on_entries": ["beta"],
        "cluster": ["alpha", "beta"], "ruled_by": "agent", "date": dt.date.today().isoformat()}
    assert alpha["entry_hash"] == hash_of(repo, "alpha")
    assert beta["passes"][0]["ruling"]["priority"] == 2
    assert beta["passes"][0]["docs"][-1]["path"] == "entries/alpha/ruling-t1-a2.md"
    assert statuses(store, repo)["alpha"][0] == "triaged"
    assert statuses(store, repo)["beta"][0] == "triaged"
    assert run(capsys, "record", "alpha", "--ruling", str(good), *opts(repo, store))[0] == 1


@pytest.mark.parametrize(("fields", "message"), [
    ("disposition: maybe\n    summary: s\n", "disposition 'maybe' is not one of"),
    ("disposition: do\n    priority: 5\n    summary: s\n", "priority must be 1-4"),
    ("disposition: defer\n    priority: 2\n    defer_until: x\n    summary: s\n",
     "priority is only for `do`"),
    ("disposition: blocked\n    blocked_on: []\n    summary: s\n", "blocked_on must be a non-empty list"),
    ("disposition: do\n    priority: 1\n    blocked_on: [x]\n    summary: s\n",
     "blocked_on is only for `blocked`"),
    ("disposition: defer\n    summary: s\n", "defer_until must be non-empty"),
    ("disposition: delete\n    defer_until: x\n    summary: s\n", "defer_until is only for"),
    ("disposition: delete\n", "summary is missing"),
])
def test_record_validation_errors(repo: Path, store: Store, capsys: pytest.CaptureFixture[str],
                                  fields: str, message: str) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    doc = ruling_doc(store, "alpha", 1, f"rulings:\n  alpha:\n    {fields}")
    code, _, err = run(capsys, "record", "alpha", "--ruling", str(doc), *opts(repo, store))
    assert code == 1 and message in err and f"{doc}:3: `alpha`: " in err


def test_record_needs_open_passes_and_frontmatter(repo: Path, store: Store,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    doc = ruling_doc(store, "alpha", 1, (
        "rulings:\n  alpha:\n    disposition: delete\n    summary: s\n"
        "  gamma:\n    disposition: delete\n    summary: s\n"))
    code, _, err = run(capsys, "record", "alpha", "--ruling", str(doc), *opts(repo, store))
    assert code == 1 and f"{doc}:6: `gamma` has no open pass" in err
    plain = write(store.entry_dir("alpha") / "ruling-t1-a2.md", "Escalation: question.\n")
    code, _, err = run(capsys, "record", "alpha", "--ruling", str(plain), *opts(repo, store))
    assert code == 1 and "no `rulings` mapping" in err
    assert len(store.load("alpha")["passes"][0]["docs"]) == 2
    code, _, err = run(capsys, "record", "beta", "--ruling", str(doc), *opts(repo, store))
    assert code == 1 and "`beta` has no open pass" in err


def test_override_closes_the_open_pass(repo: Path, store: Store,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    code, _, err = run(capsys, "override", "alpha", "--disposition", "do", *opts(repo, store))
    assert code == 1 and "priority must be 1-4" in err
    code, out, _ = run(capsys, "override", "alpha", "--disposition", "blocked", "--blocked-on",
                       "beta", "a capability", *opts(repo, store))
    assert code == 0 and out.strip() == "alpha: blocked (ruled_by: user)"
    data = store.load("alpha")
    assert len(data["passes"]) == 1
    assert data["passes"][0]["ruling"]["blocked_on"] == ["beta", "a capability"]
    assert data["passes"][0]["ruling"]["ruled_by"] == "user"
    run(capsys, "override", "gamma", "--disposition", "delete", "--summary", "no",
        *opts(repo, store))
    assert store.load("gamma")["passes"][0]["ruling"]["summary"] == "no"
    assert statuses(store, repo)["gamma"][0] == "triaged"


def commit_all(repo: Path, message: str, date: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, f"--date={date}")


def test_ready_ranks_filters_and_hides_open_attempts(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    write(repo / "TODO.md", TODO + "\n## `delta`\n\nOlder? no, newer.\n")
    commit_all(repo, "delta", "2026-03-01T00:00:00")
    for slug, priority in (("alpha", 3), ("beta", 2), ("gamma", 2), ("delta", 2)):
        store.save(slug, approved(priority=priority, entry_hash=hash_of(repo, slug)))
    store.save("gamma", approved("defer", defer_until="x", entry_hash=hash_of(repo, "gamma")))
    code, out, _ = run(capsys, "ready", *opts(repo, store))
    rows_ = [line.split("\t") for line in out.splitlines()]
    assert code == 0 and [r[0] for r in rows_] == ["beta", "delta", "alpha"]
    assert rows_[0] == ["beta", "P2", "triaged", "-", "2026-01-02", "-", "src/core", "-",
                        "-"]
    _, out, _ = run(capsys, "ready", "--priority", "3", *opts(repo, store))
    assert [line.split("\t")[0] for line in out.splitlines()] == ["alpha"]
    _, out, _ = run(capsys, "ready", "--max-priority", "2", *opts(repo, store))
    assert [line.split("\t")[0] for line in out.splitlines()] == ["beta", "delta"]
    _, out, _ = run(capsys, "ready", "--slugs", "alpha", "delta", "--exclude", "delta",
                    *opts(repo, store))
    assert [line.split("\t")[0] for line in out.splitlines()] == ["alpha"]
    data = store.load("beta")
    data["attempts"] = [{"adr_dir": "/x", "started": "2026-09-01T00:00:00", "closed": None}]
    store.save("beta", data)
    _, out, _ = run(capsys, "ready", "--max-priority", "2", *opts(repo, store))
    assert [line.split("\t")[0] for line in out.splitlines()] == ["delta"]
    code, out, _ = run(capsys, "ready", "--priority", "1", *opts(repo, store))
    assert code == 3 and out.strip() == "nothing ready"


def test_ready_offers_only_standing_rulings(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    write(repo / "TODO.md", TODO + "\n## `delta`\n\nOld ruling.\n")
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    store.save("beta", ruled(entry_hash="0" * 64))
    reopened = ruled(entry_hash=hash_of(repo, "gamma"))
    reopened["passes"].append({"pass": 2, "started": "t", "docs": []})
    store.save("gamma", reopened)
    store.save("delta", ruled(date=dt.date.today() - dt.timedelta(days=400),
                              entry_hash=hash_of(repo, "delta")))
    got = statuses(store, repo)
    assert [got[s][0] for s in ("alpha", "beta", "gamma", "delta")] == [
        "triaged", "modified", "open", "stale"]
    assert run(capsys, "ready", *opts(repo, store))[0] == 3
    for slug in ("alpha", "beta", "delta"):
        assert run(capsys, "approve", slug, *opts(repo, store))[0] == 0
    code, out, _ = run(capsys, "ready", *opts(repo, store))
    assert code == 0
    assert sorted(line.split("\t")[0] for line in out.splitlines()) == ["alpha", "delta"]


def selection(store: Store, slugs: list[str]) -> Path:
    return write(store.root / "sessions" / "selection-20260929-120000.md",
                 f"---\nslugs: [{', '.join(slugs)}]\n---\n\n# Do the thing\n\nBody.\n")


def test_attempt_and_close(repo: Path, store: Store, tmp_path: Path,
                           capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "adr"
    for slug in ("alpha", "beta"):
        store.save(slug, approved(entry_hash=hash_of(repo, slug)))
    sel = selection(store, ["alpha", "beta"])
    args = ["attempt", "--selection", str(sel), "--adr-root", str(root), *opts(repo, store)]
    code, out, _ = run(capsys, *args)
    now = dt.date.today()
    adr = root / now.strftime("%Y/%m") / f"{now.strftime('%d')}-core--todo-alpha"
    assert code == 0 and out.strip() == str(adr)
    assert not (adr / "user-request.md").exists()
    request = (adr / "request.md").read_text(encoding="utf-8")
    first = request.splitlines()[0]
    assert first == ops.DISCLAIMER
    assert PROVENANCE_LINE_RE.match(first)
    assert request.endswith("\n\n# Do the thing\n\nBody.\n")
    attempt = store.load("beta")["attempts"][0]
    assert attempt["adr_dir"] == str(adr) and attempt["closed"] is None
    code, _, err = run(capsys, *args)
    assert code == 1 and "`alpha` already has an open attempt" in err

    code, out, _ = run(capsys, "attempt-close", "--adr", str(adr), *opts(repo, store))
    assert code == 0 and out.splitlines() == ["incomplete: alpha, beta",
                                              "still in TODO.md: alpha, beta"]
    assert store.load("alpha")["attempts"][0]["outcome"] == "incomplete"
    assert store.load("alpha")["attempts"][0]["remaining"] == ["alpha", "beta"]
    code, _, err = run(capsys, "attempt-close", "--adr", str(adr), *opts(repo, store))
    assert code == 1 and "already closed" in err

    _, out, _ = run(capsys, *args)
    assert Path(out.strip()).name == f"{now.strftime('%d')}-core--todo-alpha-2"
    write(repo / "TODO.md", TODO.replace(TODO[TODO.index("## `alpha`"):TODO.index("## beta")],
                                         ""))
    assert "alpha" not in todomd.read(repo, "TODO.md").by_slug()
    assert "beta" in todomd.read(repo, "TODO.md").by_slug()
    code, out, _ = run(capsys, "attempt-close", "--adr", out.strip(), "--outcome", "stopped",
                       *opts(repo, store))
    assert out.splitlines() == ["stopped: alpha, beta", "still in TODO.md: beta"]


def test_attempt_close_shipped_when_every_entry_is_gone(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gamma", approved(entry_hash=hash_of(repo, "gamma")))
    _, out, _ = run(capsys, "attempt", "--selection", str(selection(store, ["gamma"])),
                    "--adr-root", str(tmp_path / "adr"), *opts(repo, store))
    write(repo / "TODO.md", TODO.replace(TODO[TODO.index("## `gamma`"):
                                              TODO.index("## `example")], ""))
    code, out, _ = run(capsys, "attempt-close", "--adr", out.strip(), *opts(repo, store))
    assert code == 0 and out.splitlines() == ["shipped: gamma"]
    assert store.load("gamma")["attempts"][0]["remaining"] == []
    assert run(capsys, "attempt-close", "--adr", str(tmp_path), *opts(repo, store))[0] == 1


def without_alpha() -> str:
    return TODO.replace(TODO[TODO.index("## `alpha`"):TODO.index("## beta")], "")


def test_attempt_close_dry_run(repo: Path, store: Store, tmp_path: Path,
                               capsys: pytest.CaptureFixture[str]) -> None:
    for slug in ("alpha", "beta"):
        store.save(slug, approved(entry_hash=hash_of(repo, slug)))
    _, out, _ = run(capsys, "attempt", "--selection", str(selection(store, ["alpha", "beta"])),
                    "--adr-root", str(tmp_path / "adr"), *opts(repo, store))
    adr = out.strip()
    write(repo / "TODO.md", without_alpha())
    for _ in range(2):
        code, out, _ = run(capsys, "attempt-close", "--adr", adr, "--dry-run",
                           *opts(repo, store))
        assert code == 0 and out.splitlines() == ["would close incomplete: alpha, beta",
                                                  "still in TODO.md: beta"]
        attempt = store.load("beta")["attempts"][0]
        assert attempt["closed"] is None and "remaining" not in attempt
    code, _, err = run(capsys, "attempt-close", "--adr", adr, "--dry-run", "--outcome",
                       "stopped", *opts(repo, store))
    assert code == 1 and "--dry-run takes no --outcome" in err
    code, out, _ = run(capsys, "attempt-close", "--adr", adr, "--dry-run", "--json",
                       *opts(repo, store))
    assert code == 0 and json.loads(out) == {
        "outcome": "incomplete", "slugs": ["alpha", "beta"], "remaining": ["beta"],
        "dry_run": True}
    write(repo / "TODO.md", TODO.replace(TODO[TODO.index("## `alpha`"):
                                              TODO.index("## `gamma`")], ""))
    code, out, _ = run(capsys, "attempt-close", "--adr", adr, "--dry-run", *opts(repo, store))
    assert code == 0 and out.splitlines() == ["would close shipped: alpha, beta"]
    assert run(capsys, "attempt-close", "--adr", adr, *opts(repo, store))[0] == 0
    code, _, err = run(capsys, "attempt-close", "--adr", adr, "--dry-run", *opts(repo, store))
    assert code == 1 and "already closed" in err


def test_attempt_close_lists_slugs_alphabetically(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for slug in ("alpha", "beta"):
        store.save(slug, approved(entry_hash=hash_of(repo, slug)))
    _, out, _ = run(capsys, "attempt", "--selection", str(selection(store, ["beta", "alpha"])),
                    "--adr-root", str(tmp_path / "adr"), *opts(repo, store))
    code, out, _ = run(capsys, "attempt-close", "--adr", out.strip(), *opts(repo, store))
    assert code == 0 and out.splitlines()[0] == "incomplete: alpha, beta"
    assert store.load("beta")["attempts"][0]["remaining"] == ["alpha", "beta"]


def test_ready_json_carries_remaining(repo: Path, store: Store, tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    for slug in ("alpha", "beta"):
        store.save(slug, approved(entry_hash=hash_of(repo, slug)))
    _, out, _ = run(capsys, "attempt", "--selection", str(selection(store, ["alpha", "beta"])),
                    "--adr-root", str(tmp_path / "adr"), *opts(repo, store))
    write(repo / "TODO.md", without_alpha())
    run(capsys, "attempt-close", "--adr", out.strip(), *opts(repo, store))
    code, out, _ = run(capsys, "ready", "--json", *opts(repo, store))
    beta = next(e for e in json.loads(out) if e["slug"] == "beta")
    assert code == 0 and beta["attempts"][0]["outcome"] == "incomplete"
    assert beta["attempts"][0]["remaining"] == ["beta"]


ENGINE = "x = 1  # TODO(alpha)\ny = 2  # TODO(beta) and TODO(orphan)\n"


def test_prune_dry_run_and_write(repo: Path, store: Store,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    store.save("beta", approved("delete", entry_hash=hash_of(repo, "beta")))
    store.save("example-placeholder", approved("delete"))
    code, out, _ = run(capsys, "prune", *opts(repo, store))
    assert code == 0 and out.splitlines() == [
        "TODO.md:9: `beta`", "code sites:",
        "  src/core/engine.py:2: y = 2  # TODO(beta) and TODO(orphan)",
        "no code sites of entries pruned earlier"]
    assert (repo / "TODO.md").read_text(encoding="utf-8") == TODO
    code, out, err = run(capsys, "prune", "--write", *opts(repo, store))
    assert code == 1 and out == ""
    assert err.splitlines() == [
        "todo-triage: 1 code site(s) of deleted entries remain; remove them first:",
        "  src/core/engine.py:2: y = 2  # TODO(beta) and TODO(orphan)"]
    assert (repo / "TODO.md").read_text(encoding="utf-8") == TODO
    assert "pruned" not in store.load("beta")
    write(repo / "src" / "core" / "engine.py", "x = 1  # TODO(alpha)\ny = 2  # TODO(orphan)\n")
    code, out, _ = run(capsys, "prune", "--write", *opts(repo, store))
    assert code == 0 and out.splitlines() == ["removed TODO.md:9: `beta`"]
    text = (repo / "TODO.md").read_text(encoding="utf-8")
    assert "## beta" not in text and "Unbackticked" not in text
    assert "Code site (`TODO(alpha)`): src/core/engine.py.\n\n## `gamma`" in text
    assert text.endswith("Placeholder.\n")
    assert store.load("beta")["pruned"] == dt.date.today().isoformat()
    assert statuses(store, repo)["beta"][0] == "resolved"


def test_prune_nothing_exits_3(repo: Path, store: Store,
                               capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "prune", *opts(repo, store))
    assert code == 3 and out.strip() == "nothing to prune"
    code, out, _ = run(capsys, "prune", "--json", *opts(repo, store))
    assert code == 3 and json.loads(out) == {"prunable": [], "sites": [], "leftover_sites": []}


def test_prune_lists_leftover_sites(repo: Path, store: Store,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    engine = repo / "src" / "core" / "engine.py"
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    stamped = approved("delete")
    stamped["pruned"] = dt.date.today().isoformat()
    store.save("beta", stamped)
    store.save("alpha", user_ruled(disposition="delete"))
    code, out, _ = run(capsys, "prune", *opts(repo, store))
    assert code == 0 and out.splitlines() == [
        "no code sites", "code sites of entries pruned earlier:",
        "  src/core/engine.py:1: x = 1  # TODO(alpha)",
        "  src/core/engine.py:2: y = 2  # TODO(beta) and TODO(orphan)"]
    _, out, _ = run(capsys, "prune", "--json", *opts(repo, store))
    assert [s["slug"] for s in json.loads(out)["leftover_sites"]] == ["alpha", "beta"]
    code, _, err = run(capsys, "prune", "--write", *opts(repo, store))
    assert code == 1 and "2 code site(s) of deleted entries remain" in err
    write(engine, "x = 1\ny = 2  # TODO(orphan)\n")
    code, out, _ = run(capsys, "prune", "--write", *opts(repo, store))
    assert code == 0 and out.strip() == "nothing to remove from TODO.md"
    assert run(capsys, "prune", *opts(repo, store))[0] == 3


def test_prune_leftovers_skip_unknown_ignored_and_overridden_slugs(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    engine = repo / "src" / "core" / "engine.py"
    write(repo / "TODO.md", without(TODO, "beta", "`example-placeholder`"))
    write(engine, ENGINE + "z = 3  # TODO(example-placeholder)\n")
    store.save("example-placeholder", approved("delete"))
    assert run(capsys, "prune", *opts(repo, store))[0] == 3
    store.save("beta", ruled("delete"))
    assert run(capsys, "prune", *opts(repo, store))[0] == 3
    store.save("beta", ruled("delete", review="retriage"))
    assert run(capsys, "prune", *opts(repo, store))[0] == 3
    assert run(capsys, "prune", "--write", *opts(repo, store))[0] == 0
    stamped = approved("delete")
    stamped["pruned"] = dt.date.today().isoformat()
    store.save("beta", stamped)
    code, out, _ = run(capsys, "prune", *opts(repo, store))
    assert code == 0 and "TODO(beta)" in out
    assert "TODO(orphan)" not in out.replace("TODO(beta) and TODO(orphan)", "")
    assert "example-placeholder" not in out
    code, _, err = run(capsys, "override", "beta", "--disposition", "defer", "--defer-until",
                       "later", "--summary", "the comment stays", *opts(repo, store))
    assert code == 0, err
    assert store.load("beta")["pruned"] == dt.date.today().isoformat()
    assert run(capsys, "prune", *opts(repo, store))[0] == 3
    code, out, _ = run(capsys, "prune", "--write", *opts(repo, store))
    assert code == 0 and out.strip() == "nothing to remove from TODO.md"


def test_prune_write_refuses_before_lint_check(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    broken = TODO + "\n## `alpha`\n\nduplicate\n"
    write(repo / "TODO.md", broken)
    store.save("beta", approved("delete", entry_hash=hash_of(repo, "beta")))
    code, _, err = run(capsys, "prune", "--write", *opts(repo, store))
    assert code == 1 and "1 code site(s) of deleted entries remain" in err
    assert "lint errors" not in err
    assert (repo / "TODO.md").read_text(encoding="utf-8") == broken
    assert "pruned" not in store.load("beta")


def test_prune_removes_the_last_entry_cleanly() -> None:
    from todotriage.ops import prune_text
    assert prune_text("# T\n\n## `a`\n\nx\n\n## `b`\n\ny\n\n", {7}) == "# T\n\n## `a`\n\nx\n"


def test_summary_contents(repo: Path, store: Store, tmp_path: Path,
                          capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", approved(entry_hash=hash_of(repo, "alpha")))
    store.save("gamma", ruled("blocked", blocked_on=["gone"], entry_hash=hash_of(repo, "gamma")))
    store.save("gone", ruled())
    run(capsys, "begin", "beta", *opts(repo, store))
    run(capsys, "attempt", "--selection", str(selection(store, ["alpha"])),
        "--adr-root", str(tmp_path / "adr"), *opts(repo, store))
    code, out, _ = run(capsys, "summary", *opts(repo, store))
    path = Path(out.strip())
    assert code == 0 and path.parent == store.root / "sessions"
    text = path.read_text(encoding="utf-8")
    for expected in [
        "Previous summary: (none)", "- triaged: 1", "- stale: 1", "- open: 1",
        "- resolved: 1", "- ignored: 1", "- do P2: 1 settled, 0 pending",
        "- blocked: 0 settled, 1 pending",
        "## Rulings since the start", "`alpha`: do P2,",
        "- `gamma`: blocked on `gone`, which is resolved", "- `beta`: pass 1",
        "--todo-alpha (alpha), started", "## Resolved (in the store, gone from the file)\n\n- `gone`",
        "- src/core/engine.py:2: y = 2  # TODO(beta) and TODO(orphan)",
        "warning: TODO.md:9: `beta`: slug is not backticked",
    ]:
        assert expected in text, expected
    _, out, _ = run(capsys, "summary", *opts(repo, store))
    second = Path(out.strip()).read_text(encoding="utf-8")
    assert f"Previous summary: {path}" in second
    assert f"## Rulings since {dt.date.today().isoformat()}" in second


def test_summary_names_entries_left_by_a_closed_attempt(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for slug in ("alpha", "beta"):
        store.save(slug, approved(entry_hash=hash_of(repo, slug)))
    _, out, _ = run(capsys, "attempt", "--selection", str(selection(store, ["alpha", "beta"])),
                    "--adr-root", str(tmp_path / "adr"), *opts(repo, store))
    write(repo / "TODO.md", without_alpha())
    run(capsys, "attempt-close", "--adr", out.strip(), *opts(repo, store))
    old = approved(entry_hash=hash_of(repo, "gamma"))
    old["attempts"] = [{"adr_dir": "/old", "slugs": ["gamma"], "started": "t",
                        "closed": dt.datetime.now().isoformat(timespec="seconds"),
                        "outcome": "incomplete"}]
    store.save("gamma", old)
    _, out, _ = run(capsys, "summary", *opts(repo, store))
    text = Path(out.strip()).read_text(encoding="utf-8")
    assert "(alpha, beta): incomplete, still in TODO.md: beta\n" in text
    assert "- /old (gamma): incomplete\n" in text


def test_attempt_close_names_the_configured_todo_file(
        repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "docs").mkdir()
    write(repo / "docs" / "TODO.md", TODO)
    store = Store.init(tmp_path / "docs-store", "core", "docs/TODO.md", 180,
                       ["example-placeholder"])
    for slug in ("alpha", "beta"):
        store.save(slug, approved(entry_hash=hash_of(repo, slug)))
    sel = selection(store, ["alpha", "beta"])
    _, out, _ = run(capsys, "attempt", "--selection", str(sel),
                    "--adr-root", str(tmp_path / "adr"), *opts(repo, store))
    adr = out.strip()
    write(repo / "docs" / "TODO.md", without_alpha())
    expected = ["incomplete: alpha, beta", "still in docs/TODO.md: beta"]
    _, out, _ = run(capsys, "attempt-close", "--adr", adr, "--dry-run", *opts(repo, store))
    assert out.splitlines() == [f"would close {expected[0]}", expected[1]]
    _, out, _ = run(capsys, "attempt-close", "--adr", adr, *opts(repo, store))
    assert out.splitlines() == expected
    _, out, _ = run(capsys, "summary", *opts(repo, store))
    text = Path(out.strip()).read_text(encoding="utf-8")
    assert "(alpha, beta): incomplete, still in docs/TODO.md: beta\n" in text


def test_slug_options_accept_commas(repo: Path, store: Store,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    for slug in ("alpha", "beta", "gamma"):
        store.save(slug, approved(entry_hash=hash_of(repo, slug)))
    for form in (["alpha,gamma"], ["alpha", "gamma"], ["alpha,", "gamma"]):
        _, out, _ = run(capsys, "ready", "--slugs", *form, *opts(repo, store))
        assert sorted(line.split("\t")[0] for line in out.splitlines()) == ["alpha", "gamma"]
    _, out, _ = run(capsys, "ready", "--exclude", "alpha,beta", *opts(repo, store))
    assert [line.split("\t")[0] for line in out.splitlines()] == ["gamma"]
    assert run(capsys, "ready", "--slugs", ",", *opts(repo, store))[0] == 3
    for slug in ("alpha", "beta", "gamma"):
        store.save(slug, {"slug": slug})
    _, out, _ = run(capsys, "next", "--exclude", "alpha,beta", *opts(repo, store))
    assert lines_of(out)["slug"] == "gamma"


def test_attempt_takes_only_standing_do_rulings(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    store.save("beta", ruled("delete", entry_hash=hash_of(repo, "beta")))
    store.save("gamma", ruled(entry_hash="0" * 64))
    root = tmp_path / "adr"
    code, _, err = run(capsys, "attempt", "--selection", str(selection(store, ["alpha"])),
                       "--adr-root", str(root), *opts(repo, store))
    assert code == 1 and "`alpha` is not ready: triaged, ruled do, review pending" in err
    for slug in ("alpha", "beta", "gamma"):
        run(capsys, "approve", slug, *opts(repo, store))
    for slugs, message in ((["alpha", "beta"], "`beta` is not ready: triaged, ruled delete"),
                           (["gamma"], "`gamma` is not ready: modified, ruled do"),
                           (["alpha", "example-placeholder"], "is ignored in store.yaml")):
        code, _, err = run(capsys, "attempt", "--selection", str(selection(store, slugs)),
                           "--adr-root", str(root), *opts(repo, store))
        assert code == 1 and message in err, err
    assert not root.exists()
    assert "attempts" not in store.load("alpha")


def test_contrarian_covers_follow_the_sketch(repo: Path, store: Store,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "log", "alpha", "--step", "sketch", "--doc",
        str(sketch(store, "alpha", ["alpha", "beta"])), *opts(repo, store))
    doc = write(store.entry_dir("alpha") / "contrarian-t1.md",
                "---\ncovers: [alpha, gamma]\n---\n\ngamma belongs too\n")
    code, out, _ = run(capsys, "log", "alpha", "--step", "contrarian", "--doc", str(doc),
                       *opts(repo, store))
    assert code == 0 and out.strip().endswith("-> alpha, beta")
    assert store.load("gamma") is None
    assert store.load("beta")["passes"][0]["docs"][-1]["covers"] == ["alpha", "beta"]


def test_prune_skips_delete_rulings_that_no_longer_stand(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", ruled("delete", entry_hash="0" * 64))
    reopened = ruled("delete", entry_hash=hash_of(repo, "beta"))
    reopened["passes"].append({"pass": 2, "started": "t", "docs": []})
    store.save("beta", reopened)
    store.save("gamma", ruled("delete", date=dt.date.today() - dt.timedelta(days=400),
                              entry_hash=hash_of(repo, "gamma")))
    code, out, _ = run(capsys, "prune", *opts(repo, store))
    assert code == 3 and out.strip() == "nothing to prune"
    for slug in ("alpha", "gamma"):
        assert run(capsys, "approve", slug, *opts(repo, store))[0] == 0
    _, out, _ = run(capsys, "prune", *opts(repo, store))
    assert out.splitlines()[0] == "TODO.md:13: `gamma`"
    assert "alpha" not in out and "beta" not in out


def test_prune_write_refuses_a_file_with_lint_errors(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    broken = TODO + "\n## `alpha`\n\nduplicate\n"
    write(repo / "TODO.md", broken)
    write(repo / "src" / "core" / "engine.py", "x = 1  # TODO(alpha)\n")
    store.save("beta", approved("delete", entry_hash=hash_of(repo, "beta")))
    code, _, err = run(capsys, "prune", "--write", *opts(repo, store))
    assert code == 1 and "TODO.md has lint errors; fix them before `prune --write`" in err
    assert (repo / "TODO.md").read_text(encoding="utf-8") == broken
    assert "pruned" not in store.load("beta")


def test_blocked_on_an_untriaged_entry_that_leaves_the_file_is_stale(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = run(capsys, "override", "alpha", "--disposition", "blocked", "--blocked-on",
                     "gamma", "a capability", *opts(repo, store))
    assert code == 0
    assert store.load("alpha")["passes"][0]["ruling"]["blocked_on_entries"] == ["gamma"]
    assert store.load("gamma") is None
    assert statuses(store, repo)["alpha"][0] == "triaged"
    write(repo / "TODO.md", TODO.replace(TODO[TODO.index("## `gamma`"):
                                              TODO.index("## `example")], ""))
    assert statuses(store, repo)["alpha"] == ("stale", "blocked on `gamma`, which is resolved")


def test_frontmatter_slugs_keep_their_text(repo: Path, store: Store, tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    write(repo / "TODO.md", TODO + "\n## `1234`\n\nIssue 1234.\n\n## `on`\n\nSwitch.\n")
    run(capsys, "begin", "1234", *opts(repo, store))
    doc = write(store.entry_dir("1234") / "sketch-t1.md", "---\ncovers: [1234, on]\n---\n")
    code, out, err = run(capsys, "log", "1234", "--step", "sketch", "--doc", str(doc),
                         *opts(repo, store))
    assert code == 0 and out.splitlines()[0].endswith("-> 1234, on"), err
    ruling = write(store.entry_dir("1234") / "ruling-t1-a1.md", (
        "---\nrulings:\n  1234:\n    disposition: do\n    priority: 2\n    summary: yes\n"
        "  on:\n    disposition: defer\n    defer_until: 2026-12-01\n    summary: s\n---\n"))
    code, out, err = run(capsys, "record", "1234", "--ruling", str(ruling), *opts(repo, store))
    assert code == 0 and out.strip() == "recorded: 1234, on", err
    assert store.load("1234")["passes"][0]["ruling"]["summary"] == "yes"
    assert store.load("on")["passes"][0]["ruling"]["defer_until"] == "2026-12-01"
    code, out, err = run(capsys, "approve", "1234", *opts(repo, store))
    assert code == 0 and out.splitlines()[0].startswith("1234: approved (do P2)"), err
    code, _, err = run(capsys, "attempt", "--selection", str(selection(store, ["1234"])),
                       "--adr-root", str(tmp_path / "adr"), *opts(repo, store))
    assert code == 0, err


@pytest.mark.parametrize(("frontmatter", "message"), [
    ("---\nrulings: {}\n", ":1: frontmatter opened with `---` is never closed"),
    ("---\nrulings: [unclosed\n---\n", ": invalid frontmatter YAML"),
    ("---\n- a\n- b\n---\n", ":2: frontmatter must be a mapping"),
])
def test_record_reports_malformed_frontmatter(repo: Path, store: Store,
                                              capsys: pytest.CaptureFixture[str],
                                              frontmatter: str, message: str) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    doc = write(store.entry_dir("alpha") / "ruling-t1-a1.md", frontmatter + "\nbody\n")
    code, _, err = run(capsys, "record", "alpha", "--ruling", str(doc), *opts(repo, store))
    assert code == 1 and f"{doc}{message}" in err
    assert store.load("alpha")["passes"][0]["docs"][-1]["rejected"]


def test_code_sites_with_colons_and_non_ascii_paths(repo: Path, store: Store,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    write(repo / "a:b.py", "# TODO(alpha)\n")
    write(repo / "café.py", "# TODO(alpha)\n")
    git(repo, "add", "-A")
    code, out, err = run(capsys, "sites", "alpha", *opts(repo, store))
    assert code == 0, err
    assert sorted(out.splitlines()) == ["a:b.py:1: # TODO(alpha)", "café.py:1: # TODO(alpha)",
                                        "src/core/engine.py:1: x = 1  # TODO(alpha)"]


def test_unexpected_errors_are_one_line(repo: Path, store: Store, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    def boom(*_: Any) -> None:
        raise ValueError("bad value")
    monkeypatch.setattr(todomd, "code_sites", boom)
    code, out, err = run(capsys, "lint", *opts(repo, store))
    assert code == 1 and out == ""
    assert err == "todo-triage: internal error: ValueError: bad value\n"


def test_json_output_of_list_and_ready(repo: Path, store: Store,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", approved(entry_hash=hash_of(repo, "alpha")))
    code, out, _ = run(capsys, "list", "--json", *opts(repo, store))
    listed = {r["slug"]: r for r in json.loads(out)}
    assert code == 0 and listed["alpha"] == {
        "slug": "alpha", "status": "triaged", "review": "approved", "reason": None,
        "disposition": "do",
        "priority": 2, "unblocked": False, "ruled": dt.date.today().isoformat(), "cluster": [],
        "line": 3}
    assert listed["beta"]["status"] == "untriaged" and listed["beta"]["ruled"] is None
    code, out, _ = run(capsys, "ready", "--json", *opts(repo, store))
    assert code == 0 and json.loads(out) == [{
        "slug": "alpha", "priority": 2, "status": "triaged", "disposition": "do",
        "unblocked": False, "blocked_on": [], "first_seen": "2026-01-02", "cluster": [], "site_dirs": ["src/core"], "attempts": [],
        "unlocks": []}]
    code, out, _ = run(capsys, "ready", "--json", "--priority", "1", *opts(repo, store))
    assert code == 3 and json.loads(out) == []


def test_ready_help_names_its_columns(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["ready", "--help"])
    # Undo argparse's line wrapping, which may also break after a hyphen.
    text = re.sub(r"-\s+", "-", re.sub(r"\s+", " ", capsys.readouterr().out))
    assert ("slug priority status blocked-on first-seen cluster site-dirs"
            " attempts(outcome@date,...) unlocks(path>path,...)"
            in text)


def test_abandon_returns_the_entry_to_its_prior_status(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    old = dt.date.today() - dt.timedelta(days=400)
    store.save("alpha", ruled(date=old, entry_hash=hash_of(repo, "alpha")))
    store.save("gamma", ruled(entry_hash="0" * 64))
    for slug in ("alpha", "beta", "gamma"):
        run(capsys, "begin", slug, *opts(repo, store))
    assert {s: v[0] for s, v in statuses(store, repo).items()
            if s in ("alpha", "beta", "gamma")} == {"alpha": "open", "beta": "open", "gamma": "open"}
    code, out, _ = run(capsys, "abandon", "alpha", "beta", "gamma", *opts(repo, store))
    assert code == 0 and out.splitlines() == [
        "alpha: pass 2 abandoned", "beta: pass 1 abandoned", "gamma: pass 2 abandoned"]
    after = statuses(store, repo)
    assert (after["alpha"][0], after["beta"][0], after["gamma"][0]) == (
        "stale", "untriaged", "modified")
    beta = store.load("beta")["passes"][0]
    assert beta.get("ruling") is None and beta["abandoned"]
    _, out, _ = run(capsys, "next", *opts(repo, store))
    assert lines_of(out) == {"slug": "beta", "status": "untriaged", "pass": "2", "review": "-"}
    _, out, _ = run(capsys, "begin", "beta", *opts(repo, store))
    assert lines_of(out)["pass"] == "2"


def test_abandon_is_all_or_nothing(repo: Path, store: Store,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    store.save("gamma", ruled(entry_hash=hash_of(repo, "gamma")))
    code, out, err = run(capsys, "abandon", "alpha", "beta", "gamma", *opts(repo, store))
    assert code == 1 and out == ""
    assert "no open pass to abandon: `beta`, `gamma`; nothing written" in err
    assert "abandoned" not in store.load("alpha")["passes"][0]
    assert statuses(store, repo)["alpha"][0] == "open"
    run(capsys, "abandon", "alpha", *opts(repo, store))
    code, _, err = run(capsys, "abandon", "alpha", *opts(repo, store))
    assert code == 1 and "`alpha`" in err
    code, _, err = run(capsys, "begin", "alpha", "--resume", *opts(repo, store))
    assert code == 1 and "no open pass to resume" in err


def test_abandon_and_override_leave_a_mate_open_until_the_sweep(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "log", "alpha", "--step", "sketch", "--doc",
        str(sketch(store, "alpha", ["alpha", "beta"])), *opts(repo, store))
    run(capsys, "abandon", "alpha", *opts(repo, store))
    assert statuses(store, repo)["beta"][0] == "open"
    _, out, _ = run(capsys, "next", "--exclude", "alpha", *opts(repo, store))
    assert lines_of(out)["slug"] == "gamma"

    run(capsys, "begin", "alpha", *opts(repo, store))
    code, out, _ = run(capsys, "log", "alpha", "--step", "sketch", "--doc",
                       str(sketch(store, "alpha", ["alpha", "beta"], t=2)), *opts(repo, store))
    assert code == 0 and out.splitlines()[0].endswith("-> alpha, beta")
    beta = store.load("beta")
    assert [p["pass"] for p in beta["passes"]] == [1]
    assert [d["path"] for d in beta["passes"][0]["docs"]] == [
        "entries/alpha/sketch-t1.md", "entries/alpha/sketch-t2.md"]
    run(capsys, "override", "alpha", "--disposition", "delete", *opts(repo, store))
    assert statuses(store, repo)["alpha"][0] == "triaged"
    assert statuses(store, repo)["beta"][0] == "open"
    _, out, _ = run(capsys, "next", *opts(repo, store))
    assert lines_of(out)["slug"] == "gamma"

    _, out, _ = run(capsys, "list", "--status", "open", *opts(repo, store))
    swept = [line.split("\t")[0] for line in out.splitlines()]
    assert swept == ["beta"]
    code, out, _ = run(capsys, "abandon", *swept, *opts(repo, store))
    assert code == 0 and out.strip() == "beta: pass 1 abandoned"
    assert statuses(store, repo)["beta"][0] == "untriaged"
    _, out, _ = run(capsys, "list", "--status", "open", *opts(repo, store))
    assert out == ""
    _, out, _ = run(capsys, "begin", "beta", *opts(repo, store))
    assert lines_of(out)["pass"] == "2"
    assert f"abandoned sketch: {store.root}/entries/alpha/sketch-t2.md" in out.splitlines()


def test_begin_lists_the_standing_ruling_and_the_latest_later_abandoned_pass(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    def docs(t: int, *rejected: str) -> list[dict[str, Any]]:
        out = [{"step": "sketch", "path": f"entries/alpha/sketch-t{t}.md"}]
        out += [{"step": "ruling", "path": f"entries/alpha/ruling-t{t}-a{a}.md", "rejected": r}
                for a, r in enumerate(rejected, 1)]
        return out

    data = ruled(entry_hash=hash_of(repo, "alpha"))
    data["passes"][0]["docs"] = docs(1) + [{"step": "ruling", "path": "entries/alpha/ruling-t1-a1.md"}]
    data["passes"] += [
        {"pass": 2, "started": "t", "docs": docs(2, "old"), "abandoned": "2026-01-01T00:00:00"},
        {"pass": 3, "started": "t", "docs": docs(3, "bad priority"),
         "abandoned": "2026-02-01T00:00:00"}]
    data["entry_hash"] = "0" * 64
    store.save("alpha", data)
    code, out, _ = run(capsys, "begin", "alpha", *opts(repo, store))
    root = store.root
    assert code == 0 and lines_of(out)["pass"] == "4"
    assert [line for line in out.splitlines() if line.startswith(("prior", "abandoned"))] == [
        f"prior sketch: {root}/entries/alpha/sketch-t1.md",
        f"prior ruling: {root}/entries/alpha/ruling-t1-a1.md",
        f"abandoned sketch: {root}/entries/alpha/sketch-t3.md",
        f"abandoned ruling (rejected: bad priority): {root}/entries/alpha/ruling-t3-a1.md",
    ]
    code, out, _ = run(capsys, "begin", "alpha", "--resume", "--json", *opts(repo, store))
    payload = json.loads(out)
    assert [d["path"] for d in payload["prior"]] == [
        f"{root}/entries/alpha/sketch-t1.md", f"{root}/entries/alpha/ruling-t1-a1.md"]
    assert payload["abandoned"][1] == {"step": "ruling", "rejected": "bad priority",
                                       "path": f"{root}/entries/alpha/ruling-t3-a1.md"}

    store.save("beta", {"slug": "beta", "passes": [
        {"pass": 1, "started": "t", "docs": [{"step": "sketch", "path": "entries/beta/sketch-t1.md"}],
         "abandoned": "2026-01-01T00:00:00"}]})
    assert statuses(store, repo)["beta"][0] == "untriaged"
    _, out, _ = run(capsys, "begin", "beta", *opts(repo, store))
    assert lines_of(out)["pass"] == "2"
    assert [line for line in out.splitlines() if line.startswith("prior")] == ["prior: (none)"]
    assert f"abandoned sketch: {root}/entries/beta/sketch-t1.md" in out.splitlines()

    data = ruled(entry_hash=hash_of(repo, "gamma"))
    data["passes"][0]["pass"] = 2
    data["passes"].insert(0, {"pass": 1, "started": "t", "abandoned": "2026-01-01T00:00:00",
                              "docs": [{"step": "sketch", "path": "entries/gamma/sketch-t1.md"}]})
    store.save("gamma", data)
    _, out, _ = run(capsys, "begin", "gamma", *opts(repo, store))
    assert [line for line in out.splitlines() if line.startswith("abandoned")] == [
        "abandoned: (none)"]


def test_summary_lists_abandoned_passes(repo: Path, store: Store,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "log", "alpha", "--step", "sketch", "--doc",
        str(sketch(store, "alpha", ["alpha", "beta"])), *opts(repo, store))
    for a in (1, 2):
        write(store.entry_dir("alpha") / f"ruling-t1-a{a}.md", "escalated\n")
    run(capsys, "abandon", "alpha", *opts(repo, store))
    stamp = store.load("alpha")["passes"][0]["abandoned"]
    _, out, _ = run(capsys, "summary", *opts(repo, store))
    text = Path(out.strip()).read_text(encoding="utf-8")
    assert ("## Abandoned passes since the start\n\n"
            f"- `alpha`: pass 1, abandoned {stamp}, 2 ruling attempt(s) used\n") in text
    assert "## Open passes\n\n- `beta`: pass 1\n" in text


def user_ruled(**extra: Any) -> dict[str, Any]:
    data = ruled(**extra)
    data["passes"][0]["ruling"]["ruled_by"] = "user"
    return data


def log_sketch(capsys: pytest.CaptureFixture[str], repo: Path, store: Store,
               *json_flag: str) -> tuple[int, str, str]:
    return run(capsys, "log", "alpha", "--step", "sketch", "--doc",
               str(sketch(store, "alpha", ["alpha", "beta"])), *opts(repo, store), *json_flag)


@pytest.mark.parametrize("state", ["triaged", "modified", "stale"])
def test_sketch_holds_a_user_ruled_mate(repo: Path, store: Store,
                                        capsys: pytest.CaptureFixture[str], state: str) -> None:
    beta = user_ruled(entry_hash="0" * 64 if state == "modified" else hash_of(repo, "beta"),
                      date=dt.date(2020, 1, 1) if state == "stale" else None)
    store.save("beta", beta)
    assert statuses(store, repo)["beta"][0] == state
    before = store.triage_path("beta").read_text(encoding="utf-8")
    run(capsys, "begin", "alpha", *opts(repo, store))
    code, out, _ = log_sketch(capsys, repo, store)
    date = beta["passes"][0]["ruling"]["date"]
    assert code == 0 and out.splitlines() == [
        f"sketch: {store.root}/entries/alpha/sketch-t1.md -> alpha",
        f"held beta: ruled by user {date} (do); no pass opened — "
        "`todo-triage override beta` changes it"]
    assert store.triage_path("beta").read_text(encoding="utf-8") == before
    assert store.load("alpha")["passes"][0]["docs"][-1]["covers"] == ["alpha"]


def test_sketch_takes_a_user_ruled_mate_sent_back(repo: Path, store: Store,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    store.save("beta", user_ruled(entry_hash=hash_of(repo, "beta")))
    code, _, err = run(capsys, "retriage", "beta", "--note", "Look again.", *opts(repo, store))
    assert code == 0, err
    run(capsys, "begin", "alpha", *opts(repo, store))
    code, out, _ = log_sketch(capsys, repo, store)
    assert code == 0 and out.splitlines() == [
        f"sketch: {store.root}/entries/alpha/sketch-t1.md -> alpha, beta",
        "held: (none)",
        f"review beta (retriage): {store.root}/entries/beta/review-user-t1.md"]
    assert [p["pass"] for p in store.load("beta")["passes"]] == [1, 2]
    both = ruling_doc(store, "alpha", 1, (
        "rulings:\n  alpha:\n    disposition: delete\n    summary: s\n"
        "  beta:\n    disposition: delete\n    summary: s\n"))
    code, _, err = run(capsys, "record", "alpha", "--ruling", str(both), *opts(repo, store))
    assert code == 0, err
    beta = store.load("beta")
    assert beta["passes"][-1]["ruling"]["ruled_by"] == "agent"
    assert reviews(store, repo)["beta"] == "pending"


def test_sketch_log_json_names_held_slugs(repo: Path, store: Store,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    store.save("beta", user_ruled(entry_hash=hash_of(repo, "beta")))
    run(capsys, "begin", "alpha", *opts(repo, store))
    _, out, _ = log_sketch(capsys, repo, store, "--json")
    assert json.loads(out) == [{"path": f"{store.root}/entries/alpha/sketch-t1.md",
                                "slugs": ["alpha"], "held": ["beta"], "reviews": []}]
    contrarian = write(store.entry_dir("alpha") / "contrarian-t1.md", "report\n")
    _, out, _ = run(capsys, "log", "alpha", "--step", "contrarian", "--doc", str(contrarian),
                    *opts(repo, store), "--json")
    assert json.loads(out) == [{"path": str(contrarian), "slugs": ["alpha"], "held": [],
                                "reviews": []}]


def test_sketch_mirrors_into_an_agent_ruled_mate(repo: Path, store: Store,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    store.save("beta", ruled(entry_hash=hash_of(repo, "beta")))
    run(capsys, "begin", "alpha", *opts(repo, store))
    _, out, _ = log_sketch(capsys, repo, store, "--json")
    assert json.loads(out)[0]["held"] == [] and json.loads(out)[0]["slugs"] == ["alpha", "beta"]
    assert [p["pass"] for p in store.load("beta")["passes"]] == [1, 2]
    beta = [r for r in rows(store, todomd.read(repo, "TODO.md")) if r.slug == "beta"][0]
    assert beta.review == "pending"


def test_sketch_holds_a_user_ruled_mate_with_an_open_pass(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("beta", user_ruled(entry_hash=hash_of(repo, "beta")))
    run(capsys, "begin", "beta", *opts(repo, store))
    assert statuses(store, repo)["beta"][0] == "open"
    run(capsys, "begin", "alpha", *opts(repo, store))
    code, out, _ = log_sketch(capsys, repo, store)
    assert code == 0 and "held beta: ruled by user" in out
    beta_pass = store.load("beta")["passes"][-1]
    assert beta_pass["pass"] == 2 and beta_pass["docs"] == [] and "ruling" not in beta_pass


@pytest.mark.parametrize("beta_open", [False, True])
def test_record_rejects_a_ruling_on_a_user_ruled_slug(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str], beta_open: bool) -> None:
    store.save("beta", user_ruled(entry_hash=hash_of(repo, "beta")))
    user_ruling = store.load("beta")["passes"][0]["ruling"]
    if beta_open:
        run(capsys, "begin", "beta", *opts(repo, store))
    run(capsys, "begin", "alpha", *opts(repo, store))
    log_sketch(capsys, repo, store)
    both = ruling_doc(store, "alpha", 1, (
        "rulings:\n  alpha:\n    disposition: delete\n    summary: s\n"
        "  beta:\n    disposition: delete\n    summary: s\n"))
    code, _, err = run(capsys, "record", "alpha", "--ruling", str(both), *opts(repo, store))
    assert code == 1 and err.splitlines() == [
        f"{both}:6: `beta` is ruled by the user ({user_ruling['date']}); a sketch cannot "
        "re-rule it — drop it from `rulings`, or the user runs `todo-triage override beta`"]
    assert store.load("alpha")["passes"][0]["docs"][-1]["rejected"].startswith(
        "`beta` is ruled by the user")
    beta = store.load("beta")
    assert beta["passes"][0]["ruling"] == user_ruling
    assert all(p.get("ruling") is None for p in beta["passes"][1:])
    alone = ruling_doc(store, "alpha", 2,
                       "rulings:\n  alpha:\n    disposition: delete\n    summary: s\n")
    code, out, _ = run(capsys, "record", "alpha", "--ruling", str(alone), *opts(repo, store))
    assert code == 0 and out.strip() == "recorded: alpha"
    assert store.load("beta") == beta


def test_begin_hands_over_the_users_ruling(repo: Path, store: Store,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "override", "beta", "--disposition", "do", "--priority", "3", "--summary",
        "keep it", *opts(repo, store))
    write(repo / "TODO.md", TODO.replace("Unbackticked entry", "Reworded entry"))
    assert statuses(store, repo)["beta"][0] == "modified"
    stored = store.load("beta")["passes"][0]["ruling"]
    _, out, _ = run(capsys, "begin", "beta", *opts(repo, store), "--json")
    assert json.loads(out)["user_ruling"] == stored
    run(capsys, "abandon", "beta", *opts(repo, store))
    _, out, _ = run(capsys, "begin", "beta", *opts(repo, store))
    assert f"prior ruling (user): do P3 {stored['date']}: keep it" in out.splitlines()
    assert "prior ruling:" not in out


def test_begin_prints_every_field_of_a_users_ruling(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "override", "beta", "--disposition", "blocked", "--blocked-on", "alpha",
        "a queue", *opts(repo, store))
    run(capsys, "override", "gamma", "--disposition", "defer", "--defer-until",
        "the queue lands", *opts(repo, store))
    today = dt.date.today().isoformat()
    _, out, _ = run(capsys, "begin", "beta", *opts(repo, store))
    assert f"prior ruling (user): blocked blocked_on alpha,a queue {today}: -" in out
    _, out, _ = run(capsys, "begin", "gamma", *opts(repo, store))
    assert f"prior ruling (user): defer defer_until the queue lands {today}: -" in out


def test_begin_after_an_agent_ruling_has_no_user_ruling(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("beta", ruled(entry_hash="0" * 64))
    _, out, _ = run(capsys, "begin", "beta", *opts(repo, store), "--json")
    assert json.loads(out)["user_ruling"] is None



def reviews(store: Store, repo: Path) -> dict[str, str]:
    return {r.slug: r.review for r in rows(store, todomd.read(repo, "TODO.md"))}


def rule_cluster(capsys: pytest.CaptureFixture[str], repo: Path, store: Store) -> Path:
    """alpha and beta ruled together by an agent, through the tool; returns the ruling."""
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "log", "alpha", "--step", "sketch", "--doc",
        str(sketch(store, "alpha", ["alpha", "beta"])), *opts(repo, store))
    contrarian = write(store.entry_dir("alpha") / "contrarian-t1.md", "report\n")
    run(capsys, "log", "alpha", "--step", "contrarian", "--doc", str(contrarian),
        *opts(repo, store))
    doc = ruling_doc(store, "alpha", 1, (
        "rulings:\n  alpha:\n    disposition: do\n    priority: 3\n    summary: fix it\n"
        "  beta:\n    disposition: delete\n    summary: gone\n"))
    code, _, err = run(capsys, "record", "alpha", "--ruling", str(doc), *opts(repo, store))
    assert code == 0, err
    return doc


def test_review_state_derivation(repo: Path, store: Store) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    store.save("beta", user_ruled(entry_hash=hash_of(repo, "beta")))
    store.save("gamma", approved(entry_hash=hash_of(repo, "gamma")))
    store.save("gone", ruled(review="retriage"))
    assert reviews(store, repo) == {"alpha": "pending", "beta": "approved", "gamma": "approved",
                                    "gone": "retriage", "example-placeholder": "-"}
    empty = ruled()
    empty["passes"][0]["review"] = {}
    store.save("alpha", empty)
    store.save("gamma", ruled(review="bogus"))
    assert reviews(store, repo)["alpha"] == "pending"
    assert reviews(store, repo)["gamma"] == "pending"
    store.save("beta", user_ruled(review="retriage", entry_hash=hash_of(repo, "beta")))
    assert reviews(store, repo)["beta"] == "retriage"
    for state in ("approved", "bogus"):
        store.save("beta", user_ruled(review=state, entry_hash=hash_of(repo, "beta")))
        assert reviews(store, repo)["beta"] == "approved"


def test_inbox_lists_pending_rulings_in_file_order(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "inbox", *opts(repo, store))
    assert code == 3 and out.strip() == "no rulings awaiting review"
    write(repo / "TODO.md", TODO + "\n## `delta`\n\nMid-pass.\n\n## `eps`\n\nSent back.\n")
    store.save("gamma", ruled("blocked", blocked_on=["alpha"], entry_hash=hash_of(repo, "gamma"),
                              cluster=["gamma", "beta"]))
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    abandoned = ruled(entry_hash="0" * 64, cluster=["gamma", "beta"])
    abandoned["passes"].append({"pass": 2, "started": "t", "docs": [], "abandoned": "t"})
    store.save("beta", abandoned)
    reopened = ruled(entry_hash=hash_of(repo, "delta"))
    reopened["passes"].append({"pass": 2, "started": "t", "docs": []})
    store.save("delta", reopened)
    store.save("eps", ruled(review="retriage", entry_hash=hash_of(repo, "eps")))
    store.save("example-placeholder", ruled())
    store.save("gone", ruled())
    store.save("mine", user_ruled())
    code, out, _ = run(capsys, "inbox", *opts(repo, store))
    today = dt.date.today().isoformat()
    assert code == 0 and out.splitlines() == [
        f"alpha\ttriaged\tdo P2\t-\t{today}\ts",
        f"beta\tmodified\tdo P2\tgamma\t{today}\ts",
        f"gamma\ttriaged\tblocked\tbeta\t{today}\ts",
        "3 ruling(s) awaiting review"]


@pytest.mark.parametrize("beta_now", ["separate", "open"])
def test_inbox_lists_only_mates_that_share_the_ruling(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str], beta_now: str) -> None:
    rule_cluster(capsys, repo, store)
    if beta_now == "separate":
        store.save("beta", ruled(entry_hash=hash_of(repo, "beta")))
    else:
        run(capsys, "begin", "beta", *opts(repo, store))
    code, out, _ = run(capsys, "inbox", *opts(repo, store))
    lines = {line.split("\t")[0]: line.split("\t")[3] for line in out.splitlines()[:-1]}
    assert code == 0 and lines["alpha"] == "-"
    assert lines.get("beta") == ("-" if beta_now == "separate" else None)
    _, out, _ = run(capsys, "inbox", "--json", *opts(repo, store))
    payload = {p["slug"]: p for p in json.loads(out)}
    assert payload["alpha"]["mates"] == [] and payload["alpha"]["cluster"] == ["alpha", "beta"]


def test_inbox_json_carries_the_pass_documents(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    rule_cluster(capsys, repo, store)
    code, out, _ = run(capsys, "inbox", "--json", *opts(repo, store))
    payload = {p["slug"]: p for p in json.loads(out)}
    d = store.entry_dir("alpha")
    assert code == 0 and list(payload) == ["alpha", "beta"]
    for slug in ("alpha", "beta"):
        assert payload[slug]["review"] == "pending"
        assert payload[slug]["ruling"] == f"{d}/ruling-t1-a1.md"
        assert payload[slug]["sketch"] == f"{d}/sketch-t1.md"
        assert payload[slug]["contrarian"] == f"{d}/contrarian-t1.md"
    assert payload["alpha"]["entry"].startswith("## `alpha`")
    assert payload["alpha"]["mates"] == ["beta"] and payload["beta"]["mates"] == ["alpha"]


def rule_pass(capsys: pytest.CaptureFixture[str], repo: Path, store: Store, runner: str,
              covers: list[str], explorations: int = 0) -> Path:
    """One agent pass on `runner` covering `covers`, through the tool; returns the ruling."""
    code, _, err = run(capsys, "begin", runner, *opts(repo, store))
    assert code == 0, err
    t = max(int(p["pass"]) for p in store.load(runner)["passes"])
    d = store.entry_dir(runner)
    for n in range(1, explorations + 1):
        write(d / f"exploration-t{t}-{n}.md", "report\n")
    if explorations:
        run(capsys, "log", runner, "--step", "exploration", "--scan", *opts(repo, store))
    run(capsys, "log", runner, "--step", "sketch", "--doc",
        str(sketch(store, runner, covers, t)), *opts(repo, store))
    rulings = "".join(f"  {slug}:\n    disposition: do\n    priority: 2\n    summary: s\n"
                      for slug in covers)
    doc = write(d / f"ruling-t{t}-a1.md", f"---\nrulings:\n{rulings}---\n\n## Decision\n")
    code, _, err = run(capsys, "record", runner, "--ruling", str(doc), *opts(repo, store))
    assert code == 0, err
    return doc


def inbox_payload(capsys: pytest.CaptureFixture[str], repo: Path,
                  store: Store) -> dict[str, dict[str, Any]]:
    code, out, err = run(capsys, "inbox", "--json", *opts(repo, store))
    assert code == 0, err
    return {p["slug"]: p for p in json.loads(out)}


def test_inbox_json_joins_explorations_across_the_cluster(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    rule_pass(capsys, repo, store, "beta", ["alpha", "beta"], explorations=2)
    payload = inbox_payload(capsys, repo, store)
    d = store.entry_dir("beta")
    expected = [f"{d}/exploration-t1-1.md", f"{d}/exploration-t1-2.md"]
    assert payload["alpha"]["mates"] == ["beta"]
    assert payload["alpha"]["explorations"] == expected
    assert payload["beta"]["explorations"] == expected
    assert payload["alpha"]["previous"] == [] and payload["beta"]["previous"] == []


def test_inbox_json_without_explorations_or_mates(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    rule_pass(capsys, repo, store, "alpha", ["alpha"])
    payload = inbox_payload(capsys, repo, store)
    assert payload["alpha"]["explorations"] == [] and payload["alpha"]["previous"] == []


def test_inbox_json_previous_skips_an_abandoned_pass(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    first = rule_pass(capsys, repo, store, "alpha", ["alpha"])
    run(capsys, "retriage", "alpha", "--note", "Too big.", *opts(repo, store))
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "abandon", "alpha", *opts(repo, store))
    rule_pass(capsys, repo, store, "alpha", ["alpha"], explorations=1)
    payload = inbox_payload(capsys, repo, store)
    d = store.entry_dir("alpha")
    assert payload["alpha"]["explorations"] == [f"{d}/exploration-t3-1.md"]
    assert payload["alpha"]["previous"] == [{
        "slug": "alpha", "pass": 1, "ruling": str(first), "ruled_by": "agent",
        "review": f"{d}/review-user-t1.md", "state": "retriage"}]


def test_inbox_json_previous_is_empty_when_only_an_abandoned_pass_came_before(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "abandon", "alpha", *opts(repo, store))
    rule_pass(capsys, repo, store, "alpha", ["alpha"])
    payload = inbox_payload(capsys, repo, store)
    assert store.load("alpha")["passes"][-1]["pass"] == 2
    assert payload["alpha"]["previous"] == []


def test_inbox_json_previous_after_an_override_has_no_ruling_document(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "override", "alpha", "--disposition", "delete",
                       *opts(repo, store))
    assert code == 0, err
    rule_pass(capsys, repo, store, "alpha", ["alpha"])
    payload = inbox_payload(capsys, repo, store)
    assert payload["alpha"]["previous"] == [{
        "slug": "alpha", "pass": 1, "ruling": None, "ruled_by": "user", "review": None,
        "state": None}]


def test_inbox_json_previous_carries_a_mates_retriage_note(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    beta_ruling = rule_pass(capsys, repo, store, "beta", ["beta"])
    run(capsys, "retriage", "beta", "--note", "Check the queue.", *opts(repo, store))
    rule_pass(capsys, repo, store, "alpha", ["alpha", "beta"])
    payload = inbox_payload(capsys, repo, store)
    expected = [{"slug": "beta", "pass": 1, "ruling": str(beta_ruling), "ruled_by": "agent",
                 "review": f"{store.entry_dir('beta')}/review-user-t1.md",
                 "state": "retriage"}]
    assert payload["alpha"]["mates"] == ["beta"]
    assert payload["alpha"]["previous"] == expected
    assert payload["beta"]["previous"] == expected


def test_inbox_json_previous_of_a_cluster_sent_back_together(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    first = rule_pass(capsys, repo, store, "alpha", ["alpha", "beta"])
    run(capsys, "retriage", "alpha", "--note", "Split them?", *opts(repo, store))
    rule_pass(capsys, repo, store, "alpha", ["alpha", "beta"])
    payload = inbox_payload(capsys, repo, store)
    note = f"{store.entry_dir('alpha')}/review-user-t1.md"
    previous = payload["alpha"]["previous"]
    assert [p["slug"] for p in previous] == ["alpha", "beta"]
    assert all(p["pass"] == 1 and p["ruling"] == str(first) and p["review"] == note
               and p["state"] == "retriage" for p in previous)
    assert [p["slug"] for p in payload["beta"]["previous"]] == ["beta", "alpha"]


def test_inbox_json_skips_rejected_rulings_and_explorations(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "log", "alpha", "--step", "sketch", "--doc",
        str(sketch(store, "alpha", ["alpha"])), *opts(repo, store))
    bad = ruling_doc(store, "alpha", 1, "rulings:\n  alpha:\n    disposition: do\n"
                                        "    summary: s\n")
    assert run(capsys, "record", "alpha", "--ruling", str(bad), *opts(repo, store))[0] == 1
    good = ruling_doc(store, "alpha", 2, "rulings:\n  alpha:\n    disposition: do\n"
                                         "    priority: 2\n    summary: s\n")
    assert run(capsys, "record", "alpha", "--ruling", str(good), *opts(repo, store))[0] == 0
    run(capsys, "retriage", "alpha", "--note", "Too big.", *opts(repo, store))
    rule_pass(capsys, repo, store, "alpha", ["alpha"], explorations=2)
    data = store.load("alpha")
    first = next(d for d in data["passes"][-1]["docs"] if d["step"] == "exploration")
    first["rejected"] = "off topic"
    store.save("alpha", data)
    payload = inbox_payload(capsys, repo, store)["alpha"]
    d = store.entry_dir("alpha")
    assert payload["explorations"] == [f"{d}/exploration-t2-2.md"]
    assert [p["ruling"] for p in payload["previous"]] == [str(good)]


def test_inbox_json_leaves_out_a_mate_ruled_separately_since(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    rule_pass(capsys, repo, store, "beta", ["alpha", "beta"], explorations=1)
    rule_pass(capsys, repo, store, "beta", ["beta"], explorations=1)
    payload = inbox_payload(capsys, repo, store)
    assert payload["alpha"]["mates"] == []
    assert payload["alpha"]["explorations"] == [] and payload["alpha"]["previous"] == []
    assert payload["beta"]["explorations"] == [f"{store.entry_dir('beta')}/exploration-t2-1.md"]
    assert [p["slug"] for p in payload["beta"]["previous"]] == ["beta"]


def test_cluster_pass_history_lists_a_shared_exploration_once() -> None:
    def member(slug: str, paths: list[str]) -> Row:
        docs = [{"step": "exploration", "path": p} for p in paths]
        record = {"pass": 1, "ruling": {"ruled_by": "agent"}, "docs": docs}
        return Row(slug, Status.TRIAGED, None, {"slug": slug, "passes": [record]})

    shared, own = "entries/alpha/exploration-t1-1.md", "entries/beta/exploration-t1-1.md"
    explorations, previous = ops.cluster_pass_history(
        member("alpha", [shared]), [member("beta", [shared, own])])
    assert explorations == [shared, own] and previous == []


def test_approve_with_and_without_a_note(repo: Path, store: Store,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    today = dt.date.today().isoformat()
    code, _, err = run(capsys, "approve", "alpha", *opts(repo, store))
    assert code == 1 and "`alpha` has no ruling to review" in err
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    code, out, _ = run(capsys, "approve", "alpha", *opts(repo, store))
    assert code == 0 and out.splitlines() == [f"alpha: approved (do P2) {today}"]
    record = store.load("alpha")["passes"][0]
    assert record["review"] == {"state": "approved", "date": today} and record["docs"] == []
    code, out, _ = run(capsys, "approve", "alpha", "--note", "Keep it small.\nTwo lines.",
                       *opts(repo, store))
    assert code == 0
    notes = store.entry_dir("alpha") / "review-user-t1.md"
    assert notes.read_text(encoding="utf-8") == (
        f"# Review of alpha pass 1, {today}: approved\n\nKeep it small.\nTwo lines.\n")
    assert store.load("alpha")["passes"][0]["docs"] == [
        {"step": "review", "path": "entries/alpha/review-user-t1.md", "date": today}]
    _, out, _ = run(capsys, "show", "alpha", *opts(repo, store))
    assert f"  t1 review: {notes}" in out.splitlines()

    run(capsys, "begin", "alpha", *opts(repo, store))
    code, _, err = run(capsys, "approve", "alpha", *opts(repo, store))
    assert code == 1 and "pass 2 is open; wait for its ruling, or `todo-triage abandon alpha`" in err
    store.save("beta", user_ruled(entry_hash=hash_of(repo, "beta")))
    code, _, err = run(capsys, "approve", "beta", *opts(repo, store))
    assert code == 1 and "`beta` is ruled by the user" in err


def test_retriage_on_a_user_ruling(repo: Path, store: Store,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    today = dt.date.today().isoformat()
    code, _, err = run(capsys, "override", "beta", "--disposition", "blocked", "--blocked-on",
                       "fltk x", *opts(repo, store))
    assert code == 0, err
    code, out, err = run(capsys, "retriage", "beta", "--note", "fltk x shipped",
                         *opts(repo, store))
    assert code == 0, err
    assert out.splitlines() == ["beta: re-triage requested; next pass 2"]
    notes = store.entry_dir("beta") / "review-user-t1.md"
    assert notes.read_text(encoding="utf-8") == (
        f"# Review of beta pass 1, {today}: retriage\n\nfltk x shipped\n")
    record = store.load("beta")["passes"][0]
    assert record["ruling"]["ruled_by"] == "user"
    assert record["review"] == {"state": "retriage", "date": today}
    assert record["docs"] == [
        {"step": "review", "path": "entries/beta/review-user-t1.md", "date": today}]
    _, out, _ = run(capsys, "list", *opts(repo, store))
    assert rows_by_slug(out)["beta"][:4] == ["beta", "triaged", "retriage", "blocked"]

    code, _, err = run(capsys, "approve", "beta", *opts(repo, store))
    assert code == 1 and "`beta` is ruled by the user" in err
    assert store.load("beta")["passes"][0]["review"]["state"] == "retriage"

    _, out, _ = run(capsys, "next", *opts(repo, store))
    assert lines_of(out)["slug"] == "beta" and lines_of(out)["review"] == "retriage"
    code, out, err = run(capsys, "begin", "beta", *opts(repo, store))
    assert code == 0, err
    lines = out.splitlines()
    assert f"prior ruling (user): blocked blocked_on fltk x {today}: -" in lines
    assert f"prior review: {notes}" in lines
    assert f"prior review (retriage): {today}" in lines
    run(capsys, "log", "beta", "--step", "sketch", "--doc",
        str(sketch(store, "beta", ["beta"], 2)), *opts(repo, store))
    doc = write(store.entry_dir("beta") / "ruling-t2-a1.md",
                "---\nrulings:\n  beta:\n    disposition: do\n    priority: 2\n"
                "    summary: now\n---\n\n## Decision\n")
    code, _, err = run(capsys, "record", "beta", "--ruling", str(doc), *opts(repo, store))
    assert code == 0, err
    assert reviews(store, repo)["beta"] == "pending"
    assert "beta" in inbox_payload(capsys, repo, store)


def test_override_takes_back_a_retriage_on_a_user_ruling(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "override", "beta", "--disposition", "blocked", "--blocked-on", "fltk x",
        *opts(repo, store))
    run(capsys, "retriage", "beta", "--note", "oops", *opts(repo, store))
    assert reviews(store, repo)["beta"] == "retriage"
    code, _, err = run(capsys, "override", "beta", "--disposition", "do", "--priority", "3",
                       *opts(repo, store))
    assert code == 0, err
    _, out, _ = run(capsys, "list", *opts(repo, store))
    assert rows_by_slug(out)["beta"][:5] == ["beta", "triaged", "approved", "do", "P3"]


def test_approve_after_an_abandoned_pass_reviews_the_ruled_pass(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "abandon", "alpha", *opts(repo, store))
    code, _, err = run(capsys, "approve", "alpha", *opts(repo, store))
    assert code == 0, err
    first, second = store.load("alpha")["passes"]
    assert first["review"]["state"] == "approved" and "review" not in second
    assert reviews(store, repo)["alpha"] == "approved"


def test_review_acts_on_the_whole_cluster(repo: Path, store: Store,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    rule_cluster(capsys, repo, store)
    today = dt.date.today().isoformat()
    code, out, _ = run(capsys, "approve", "alpha", "--note", "fine", *opts(repo, store))
    assert code == 0 and out.splitlines() == [f"alpha: approved (do P3) {today}",
                                              f"beta: approved (delete) {today}"]
    doc = {"step": "review", "path": "entries/alpha/review-user-t1.md", "date": today}
    for slug in ("alpha", "beta"):
        assert store.load(slug)["passes"][0]["docs"][-1] == doc
    assert reviews(store, repo)["beta"] == "approved"

    code, out, _ = run(capsys, "retriage", "beta", "--note", "why delete?", *opts(repo, store))
    assert code == 0 and out.splitlines() == ["beta: re-triage requested; next pass 2",
                                              "alpha: re-triage requested; next pass 2"]
    assert (store.entry_dir("beta") / "review-user-t1.md").is_file()
    assert reviews(store, repo)["alpha"] == "retriage"
    _, out, _ = run(capsys, "next", *opts(repo, store))
    assert lines_of(out) == {"slug": "alpha", "status": "triaged", "pass": "2",
                             "review": "retriage"}
    _, out, _ = run(capsys, "next", "--exclude", "alpha", *opts(repo, store))
    assert lines_of(out)["slug"] == "beta"


def test_review_leaves_a_mate_ruled_separately(repo: Path, store: Store,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    rule_cluster(capsys, repo, store)
    run(capsys, "override", "beta", "--disposition", "defer", "--defer-until", "later",
        *opts(repo, store))
    assert reviews(store, repo)["alpha"] == "pending"
    before = store.load("beta")
    code, out, _ = run(capsys, "approve", "alpha", *opts(repo, store))
    assert code == 0 and out.splitlines()[1] == (
        f"beta: ruled separately since {dt.date.today().isoformat()}; not changed")
    assert store.load("beta") == before

    store.save("gamma", ruled(entry_hash=hash_of(repo, "gamma"), cluster=["gamma", "beta"]))
    store.save("beta", ruled(entry_hash=hash_of(repo, "beta")))
    run(capsys, "approve", "gamma", *opts(repo, store))
    assert reviews(store, repo)["beta"] == "pending"


def test_review_leaves_a_mate_with_an_open_pass(repo: Path, store: Store,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    rule_cluster(capsys, repo, store)
    run(capsys, "begin", "beta", *opts(repo, store))
    before = store.load("beta")
    code, out, _ = run(capsys, "approve", "alpha", "--json", *opts(repo, store))
    assert code == 0 and json.loads(out)["unchanged"] == ["beta"]
    assert store.load("beta") == before
    assert reviews(store, repo)["alpha"] == "approved"
    code, out, _ = run(capsys, "retriage", "alpha", "--note", "again", *opts(repo, store))
    assert code == 0 and out.splitlines()[1] == "beta: pass 2 is open; not changed"
    assert store.load("beta") == before


def test_review_reports_a_mate_with_no_ruling(repo: Path, store: Store,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gamma", ruled(entry_hash=hash_of(repo, "gamma"), cluster=["gamma", "delta"]))
    code, out, _ = run(capsys, "approve", "gamma", "--json", *opts(repo, store))
    assert code == 0
    data = json.loads(out)
    assert data["changed"] == ["gamma"] and data["unchanged"] == ["delta"]
    code, out, _ = run(capsys, "retriage", "gamma", "--note", "again", *opts(repo, store))
    assert code == 0 and out.splitlines() == ["gamma: re-triage requested; next pass 2",
                                              "delta: no ruling in store; not changed"]
    assert store.load("delta") is None


def test_override_on_one_mate_leaves_the_other_pending(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    rule_cluster(capsys, repo, store)
    run(capsys, "override", "alpha", "--disposition", "do", "--priority", "1", *opts(repo, store))
    assert reviews(store, repo)["alpha"] == "approved"
    assert reviews(store, repo)["beta"] == "pending"
    _, out, _ = run(capsys, "inbox", *opts(repo, store))
    assert [line.split("\t")[0] for line in out.splitlines()[:-1]] == ["beta"]


def test_show_prints_the_review(repo: Path, store: Store,
                                capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha"), review="retriage"))
    _, out, _ = run(capsys, "show", "alpha", *opts(repo, store))
    assert out.splitlines()[1:3] == ["status: triaged", "review: retriage"]
    _, out, _ = run(capsys, "show", "alpha", "--json", *opts(repo, store))
    assert json.loads(out)["review"] == "retriage"


def test_retriage_needs_a_note_and_an_entry(repo: Path, store: Store,
                                            capsys: pytest.CaptureFixture[str],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    store.save("gone", ruled())
    store.save("example-placeholder", ruled())
    with pytest.raises(SystemExit) as exc:
        main(["retriage", "alpha", *opts(repo, store)])
    assert exc.value.code == 2 and "--note" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["retriage", "alpha", "--note", "x", "--notes-file", "-", *opts(repo, store)])
    assert exc.value.code == 2 and "not allowed with" in capsys.readouterr().err
    code, _, err = run(capsys, "retriage", "alpha", "--note", "  ", *opts(repo, store))
    assert code == 1 and "a re-triage needs a note" in err
    code, _, err = run(capsys, "retriage", "gone", "--note", "x", *opts(repo, store))
    assert code == 1 and "`gone` is not an entry in TODO.md" in err
    code, _, err = run(capsys, "retriage", "example-placeholder", "--note", "x",
                       *opts(repo, store))
    assert code == 1 and "is ignored in store.yaml" in err
    assert reviews(store, repo)["alpha"] == "pending"

    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("Is `beta` the same bug?\n"))
    code, out, _ = run(capsys, "retriage", "alpha", "--notes-file", "-", *opts(repo, store))
    assert code == 0 and out.strip() == "alpha: re-triage requested; next pass 2"
    first = store.entry_dir("alpha") / "review-user-t1.md"
    assert first.read_text(encoding="utf-8").endswith("\n\nIs `beta` the same bug?\n")
    notes = write(repo.parent / "notes.txt", "Also gamma.\n")
    run(capsys, "retriage", "alpha", "--notes-file", str(notes), *opts(repo, store))
    second = store.entry_dir("alpha") / "review-user-t1-2.md"
    assert second.read_text(encoding="utf-8").endswith("\n\nAlso gamma.\n")
    run(capsys, "approve", "alpha", *opts(repo, store))
    record = store.load("alpha")["passes"][0]
    assert record["review"]["state"] == "approved"
    assert [d["path"] for d in record["docs"]] == [
        "entries/alpha/review-user-t1.md", "entries/alpha/review-user-t1-2.md"]


def test_next_offers_a_retriage_first_and_begin_hands_it_over(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gamma", ruled(entry_hash=hash_of(repo, "gamma")))
    run(capsys, "retriage", "gamma", "--note", "why?", *opts(repo, store))
    _, out, _ = run(capsys, "next", *opts(repo, store))
    assert lines_of(out) == {"slug": "gamma", "status": "triaged", "pass": "2",
                             "review": "retriage"}
    today = dt.date.today().isoformat()
    _, out, _ = run(capsys, "begin", "gamma", *opts(repo, store))
    assert [line for line in out.splitlines() if line.startswith("prior")] == [
        f"prior review: {store.entry_dir('gamma')}/review-user-t1.md",
        f"prior review (retriage): {today}"]
    _, out, _ = run(capsys, "begin", "gamma", "--resume", "--json", *opts(repo, store))
    assert json.loads(out)["prior_review"] == {"state": "retriage", "date": today}
    assert reviews(store, repo)["gamma"] == "retriage"
    _, out, _ = run(capsys, "next", *opts(repo, store))
    assert lines_of(out)["slug"] == "alpha"

    run(capsys, "log", "gamma", "--step", "sketch", "--doc",
        str(sketch(store, "gamma", ["gamma"], t=2)), *opts(repo, store))
    doc = write(store.entry_dir("gamma") / "ruling-t2-a1.md",
                "---\nrulings:\n  gamma:\n    disposition: defer\n    defer_until: later\n"
                "    summary: answered\n---\n")
    code, _, err = run(capsys, "record", "gamma", "--ruling", str(doc), *opts(repo, store))
    assert code == 0, err
    assert reviews(store, repo)["gamma"] == "pending"
    _, out, _ = run(capsys, "inbox", *opts(repo, store))
    assert "gamma" in [line.split("\t")[0] for line in out.splitlines()[:-1]]
    _, out, _ = run(capsys, "next", *opts(repo, store))
    assert lines_of(out)["slug"] == "alpha"


@pytest.mark.parametrize("state", ["triaged", "modified", "stale"])
def test_next_offers_a_retriage_before_an_earlier_untriaged_entry(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str], state: str) -> None:
    store.save("gamma", ruled(review="retriage",
                              entry_hash="0" * 64 if state == "modified" else hash_of(repo, "gamma"),
                              date=dt.date(2020, 1, 1) if state == "stale" else None))
    assert statuses(store, repo)["alpha"][0] == "untriaged"
    _, out, _ = run(capsys, "next", *opts(repo, store))
    assert lines_of(out) == {"slug": "gamma", "status": state, "pass": "2",
                             "review": "retriage"}


def test_sketch_reports_a_covered_mates_retriage_notes(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("beta", ruled(entry_hash=hash_of(repo, "beta")))
    run(capsys, "retriage", "beta", "--note", "Same work as alpha?", *opts(repo, store))
    notes = store.entry_dir("beta") / "review-user-t1.md"
    run(capsys, "begin", "alpha", *opts(repo, store))
    code, out, _ = log_sketch(capsys, repo, store)
    assert code == 0 and out.splitlines()[1:] == [
        "held: (none)", f"review beta (retriage): {notes}"]
    contrarian = write(store.entry_dir("alpha") / "contrarian-t1.md", "report\n")
    _, out, _ = run(capsys, "log", "alpha", "--step", "contrarian", "--doc", str(contrarian),
                    *opts(repo, store), "--json")
    assert json.loads(out)[0]["reviews"] == []
    doc = ruling_doc(store, "alpha", 1, (
        "rulings:\n  alpha:\n    disposition: do\n    priority: 3\n    summary: s\n"
        "  beta:\n    disposition: do\n    priority: 3\n    summary: s\n"))
    code, _, err = run(capsys, "record", "alpha", "--ruling", str(doc), *opts(repo, store))
    assert code == 0, err
    assert reviews(store, repo)["beta"] == "pending"


def test_next_skips_a_retriage_it_cannot_begin(repo: Path, store: Store,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gone", ruled(review="retriage"))
    store.save("example-placeholder", ruled(review="retriage"))
    reopened = ruled(review="retriage", entry_hash=hash_of(repo, "beta"))
    reopened["passes"].append({"pass": 2, "started": "t", "docs": []})
    store.save("beta", reopened)
    _, out, _ = run(capsys, "next", *opts(repo, store))
    assert lines_of(out)["slug"] == "alpha"
    _, out, _ = run(capsys, "list", "--review", "retriage", *opts(repo, store))
    assert [line.split("\t")[0] for line in out.splitlines()] == [
        "beta", "example-placeholder", "gone"]


def test_ready_and_prune_take_only_settled_rulings(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha"), review="retriage"))
    store.save("beta", user_ruled(entry_hash=hash_of(repo, "beta")))
    store.save("gamma", approved(entry_hash=hash_of(repo, "gamma")))
    _, out, _ = run(capsys, "ready", *opts(repo, store))
    assert sorted(line.split("\t")[0] for line in out.splitlines()) == ["beta", "gamma"]
    store.save("alpha", ruled("delete", entry_hash=hash_of(repo, "alpha"), review="retriage"))
    store.save("gamma", ruled("delete", entry_hash=hash_of(repo, "gamma")))
    code, out, _ = run(capsys, "prune", *opts(repo, store))
    assert code == 3 and out.strip() == "nothing to prune"
    store.save("beta", user_ruled(entry_hash=hash_of(repo, "beta"), review="retriage"))
    store.save("gamma", approved(entry_hash=hash_of(repo, "gamma")))
    _, out, _ = run(capsys, "ready", *opts(repo, store))
    assert [line.split("\t")[0] for line in out.splitlines()] == ["gamma"]
    store.save("beta", user_ruled(disposition="delete", entry_hash=hash_of(repo, "beta"),
                                  review="retriage"))
    code, out, _ = run(capsys, "prune", *opts(repo, store))
    assert code == 3 and out.strip() == "nothing to prune"


def test_sketch_holds_an_approved_mate(repo: Path, store: Store,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    store.save("beta", approved(entry_hash=hash_of(repo, "beta")))
    review_date = store.load("beta")["passes"][0]["review"]["date"]
    before = store.triage_path("beta").read_text(encoding="utf-8")
    run(capsys, "begin", "alpha", *opts(repo, store))
    code, out, _ = log_sketch(capsys, repo, store)
    assert code == 0 and out.splitlines()[1] == (
        f"held beta: approved by user {review_date} (do); no pass opened — "
        "`todo-triage override beta` changes it")
    assert store.triage_path("beta").read_text(encoding="utf-8") == before
    both = ruling_doc(store, "alpha", 1, (
        "rulings:\n  alpha:\n    disposition: delete\n    summary: s\n"
        "  beta:\n    disposition: delete\n    summary: s\n"))
    code, _, err = run(capsys, "record", "alpha", "--ruling", str(both), *opts(repo, store))
    assert code == 1 and f"`beta` is approved by the user ({review_date}); a sketch cannot " \
        "re-rule it" in err


def test_related_prints_none_and_scores_mentions(repo: Path, store: Store,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    write(repo / "TODO.md", TODO + "\n## `delta`\n\nSame root cause as `gamma`.\n")
    _, out, _ = run(capsys, "related", "delta", *opts(repo, store))
    assert out.splitlines() == ["gamma\t4\tuntriaged\tmentions"]
    _, out, _ = run(capsys, "related", "gamma", *opts(repo, store))
    assert out.splitlines() == ["delta\t4\tuntriaged\tmentions"]
    write(repo / "TODO.md", TODO + "\n## `delta`\n\nUnrelated.\n")
    _, out, _ = run(capsys, "related", "delta", *opts(repo, store))
    assert out.strip() == "(none)"


def test_list_review_column_and_filter(repo: Path, store: Store,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    store.save("beta", approved(entry_hash=hash_of(repo, "beta")))
    _, out, _ = run(capsys, "list", *opts(repo, store))
    assert [line.split("\t")[2] for line in out.splitlines()] == ["pending", "approved", "-", "-"]
    _, out, _ = run(capsys, "list", "--review", "pending", "approved", *opts(repo, store))
    assert [line.split("\t")[0] for line in out.splitlines()] == ["alpha", "beta"]


def test_summary_review_sections(repo: Path, store: Store,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    store.save("beta", approved(entry_hash=hash_of(repo, "beta")))
    store.save("gamma", ruled(entry_hash=hash_of(repo, "gamma")))
    run(capsys, "retriage", "gamma", "--note", "why?", *opts(repo, store))
    _, out, _ = run(capsys, "summary", *opts(repo, store))
    text = Path(out.strip()).read_text(encoding="utf-8")
    today = dt.date.today().isoformat()
    assert "- do P2: 1 settled, 2 pending\n" in text
    assert f"## Awaiting review\n\n- alpha\ttriaged\tdo P2\t-\t{today}\ts\n" in text
    assert (f"## Re-triage requested\n\n- `gamma`: {today}, notes "
            f"{store.entry_dir('gamma')}/review-user-t1.md\n") in text


@pytest.mark.parametrize("where", ["before", "after", "split"])
def test_global_options_in_either_position(repo: Path, store: Store,
                                           capsys: pytest.CaptureFixture[str],
                                           where: str) -> None:
    store.save("alpha", ruled(entry_hash=hash_of(repo, "alpha")))
    argv = {"before": [*opts(repo, store), "--json", "show", "alpha"],
            "after": ["show", "alpha", *opts(repo, store), "--json"],
            "split": [*opts(repo, store), "show", "alpha", "--json"]}[where]
    code, out, err = run(capsys, *argv)
    assert code == 0, err
    assert json.loads(out)["review"] == "pending"


def without(text: str, *headings: str) -> str:
    """`text` with the block under each `## <heading>` line removed."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    drop: set[int] = set()
    for index, start in enumerate(starts):
        if lines[start][3:].split(" ")[0] in headings:
            stop = starts[index + 1] if index + 1 < len(starts) else len(lines)
            drop.update(range(start, stop))
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


def blocked_p2(repo: Path, review: str | None = "approved", **kwargs: Any) -> dict[str, Any]:
    """`gamma` ruled blocked on `alpha` and `beta` at P2."""
    fields: dict[str, Any] = {"priority": 2, "blocked_on": ["alpha", "beta"],
                              "blocked_on_entries": ["alpha", "beta"],
                              "entry_hash": hash_of(repo, "gamma"), **kwargs}
    return ruled("blocked", review=review, **fields)


def test_validate_ruling_takes_an_optional_priority_on_blocked() -> None:
    ruling, errors = ops.validate_ruling(
        {"disposition": "blocked", "blocked_on": ["alpha"], "priority": 2, "summary": "s"})
    assert errors == [] and ruling["priority"] == 2 and ruling["blocked_on"] == ["alpha"]
    ruling, errors = ops.validate_ruling(
        {"disposition": "blocked", "blocked_on": ["alpha"], "summary": "s"})
    assert errors == [] and ruling["priority"] is None
    _, errors = ops.validate_ruling(
        {"disposition": "blocked", "blocked_on": ["alpha"], "priority": 7, "summary": "s"})
    assert errors == ["priority must be 1-4 when given for `blocked`, got 7"]
    _, errors = ops.validate_ruling(
        {"disposition": "defer", "defer_until": "x", "priority": 2, "summary": "s"})
    assert errors == ["priority is only for `do` or `blocked`, got 2 with 'defer'"]
    _, errors = ops.validate_ruling({"disposition": "delete", "priority": 1, "summary": "s"})
    assert errors == ["priority is only for `do` or `blocked`, got 1 with 'delete'"]
    _, errors = ops.validate_ruling({"disposition": "do", "summary": "s"})
    assert errors == ["priority must be 1-4 for `do`, got None"]


def test_record_a_blocked_ruling_with_a_priority(repo: Path, store: Store,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "alpha", *opts(repo, store))
    run(capsys, "log", "alpha", "--step", "sketch", "--doc",
        str(sketch(store, "alpha", ["alpha", "beta"])), *opts(repo, store))
    head = "rulings:\n  alpha:\n    disposition: do\n    priority: 2\n    summary: s\n"
    for a, blocked_on, message in (
            (1, '["a capability"]', 'every blocked_on to be an entry: '
                                    '`a capability` is not an entry in TODO.md'),
            (2, "[beta]", 'every blocked_on to be another entry; "beta" blocks on itself')):
        doc = ruling_doc(store, "alpha", a, head + (
            f"  beta:\n    disposition: blocked\n    blocked_on: {blocked_on}\n"
            "    priority: 2\n    summary: s\n"))
        code, _, err = run(capsys, "record", "alpha", "--ruling", str(doc), *opts(repo, store))
        assert code == 1 and f"{doc}:7: `beta`: priority on blocked needs {message}" in err, err
        assert store.load("alpha")["passes"][0].get("ruling") is None
        assert store.load("beta")["passes"][0].get("ruling") is None
    good = ruling_doc(store, "alpha", 3, head + (
        "  beta:\n    disposition: blocked\n    blocked_on: [alpha]\n"
        "    priority: 2\n    summary: after alpha\n"))
    code, out, err = run(capsys, "record", "alpha", "--ruling", str(good), *opts(repo, store))
    assert code == 0 and out.strip() == "recorded: alpha, beta", err
    ruling = store.load("beta")["passes"][0]["ruling"]
    assert (ruling["priority"], ruling["blocked_on"], ruling["blocked_on_entries"]) == (
        2, ["alpha"], ["alpha"])


def test_record_a_self_block_without_a_priority(repo: Path, store: Store,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "begin", "beta", *opts(repo, store))
    doc = write(store.entry_dir("beta") / "ruling-t1-a1.md",
                "---\nrulings:\n  beta:\n    disposition: blocked\n    blocked_on: [beta]\n"
                "    summary: s\n---\n")
    code, _, err = run(capsys, "record", "beta", "--ruling", str(doc), *opts(repo, store))
    assert code == 0, err
    assert store.load("beta")["passes"][0]["ruling"]["priority"] is None


def test_override_blocked_with_a_priority(repo: Path, store: Store,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "override", "gamma", "--disposition", "blocked", "--blocked-on",
                       "alpha", "a capability", "--priority", "2", *opts(repo, store))
    assert code == 1 and "`a capability` is not an entry in TODO.md" in err
    code, _, err = run(capsys, "override", "gamma", "--disposition", "blocked", "--blocked-on",
                       "alpha", "example-placeholder", "--priority", "2", *opts(repo, store))
    assert code == 1 and "`example-placeholder` is ignored in store.yaml" in err
    assert store.load("gamma") is None
    code, _, err = run(capsys, "override", "gamma", "--disposition", "blocked", "--blocked-on",
                       "gamma", "--priority", "2", *opts(repo, store))
    assert code == 1 and '"gamma" blocks on itself' in err
    code, out, err = run(capsys, "override", "gamma", "--disposition", "blocked", "--blocked-on",
                         "alpha", "--priority", "2", *opts(repo, store))
    assert code == 0 and out.strip() == "gamma: blocked (ruled_by: user)", err
    assert store.load("gamma")["passes"][0]["ruling"]["priority"] == 2


def unblocking(store: Store, repo: Path, on: dt.date | None = None) -> tuple[str, str, bool]:
    row = {r.slug: r for r in rows(store, todomd.read(repo, "TODO.md"), on)}["gamma"]
    return row.status, row.reason, row.unblocked


def test_blocked_with_a_priority_unblocks_when_every_blocker_leaves(
        repo: Path, store: Store) -> None:
    store.save("gamma", blocked_p2(repo))
    assert unblocking(store, repo) == ("triaged", "", False)
    write(repo / "TODO.md", without(TODO, "`alpha`"))
    assert unblocking(store, repo) == ("triaged", "", False)
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    assert unblocking(store, repo) == (
        "triaged", "blocked on `alpha`, `beta`, resolved; ready at P2", True)
    row = {r.slug: r for r in rows(store, todomd.read(repo, "TODO.md"))}["gamma"]
    assert (row.disposition, row.effective_disposition, row.priority) == ("blocked", "do", 2)
    later = dt.date.today() + dt.timedelta(days=181)
    status, reason, unblocked = unblocking(store, repo, later)
    assert status == "stale" and reason.endswith("older than 180 days") and unblocked


def test_blocked_with_a_priority_unblocks_when_ruled_blockers_resolve(
        repo: Path, store: Store) -> None:
    store.save("gamma", blocked_p2(repo))
    store.save("alpha", approved(entry_hash=hash_of(repo, "alpha")))
    write(repo / "TODO.md", without(TODO, "`alpha`"))
    assert unblocking(store, repo) == ("triaged", "", False)
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    assert unblocking(store, repo) == (
        "triaged", "blocked on `alpha`, `beta`, resolved; ready at P2", True)
    store.save("beta", approved(entry_hash=""))
    assert unblocking(store, repo) == (
        "triaged", "blocked on `alpha`, `beta`, resolved; ready at P2", True)


def test_blocked_with_a_priority_on_a_deleted_blocker_is_stale(
        repo: Path, store: Store) -> None:
    store.save("gamma", blocked_p2(repo))
    store.save("alpha", ruled("delete", entry_hash=hash_of(repo, "alpha")))
    assert unblocking(store, repo) == ("triaged", "", False)
    store.save("alpha", approved("delete", entry_hash=hash_of(repo, "alpha")))
    assert unblocking(store, repo) == ("stale", "blocked on `alpha`, which is ruled delete", False)
    store.save("alpha", ruled("delete", ruled_by="user", entry_hash=hash_of(repo, "alpha")))
    assert unblocking(store, repo) == ("stale", "blocked on `alpha`, which is ruled delete", False)
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    assert unblocking(store, repo) == ("stale", "blocked on `alpha`, which is ruled delete", False)


def test_blocked_with_a_priority_on_a_pending_delete_removed_by_hand_unblocks(
        repo: Path, store: Store) -> None:
    store.save("gamma", blocked_p2(repo))
    store.save("alpha", ruled("delete", entry_hash=hash_of(repo, "alpha")))
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    assert unblocking(store, repo) == (
        "triaged", "blocked on `alpha`, `beta`, resolved; ready at P2", True)


def test_blocked_with_a_priority_on_a_sent_back_delete_stays_triaged(
        repo: Path, store: Store) -> None:
    store.save("gamma", blocked_p2(repo))
    store.save("alpha", ruled("delete", review="retriage", entry_hash=hash_of(repo, "alpha")))
    assert unblocking(store, repo) == ("triaged", "", False)
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    assert unblocking(store, repo) == (
        "triaged", "blocked on `alpha`, `beta`, resolved; ready at P2", True)


def test_blocked_with_a_priority_goes_stale_when_a_hand_removed_delete_is_approved_as_a_mate(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gamma", blocked_p2(repo, blocked_on=["alpha"], blocked_on_entries=["alpha"]))
    for slug in ("alpha", "beta"):
        store.save(slug, ruled("delete", entry_hash=hash_of(repo, slug), cluster=["alpha", "beta"]))
    write(repo / "TODO.md", without(TODO, "`alpha`"))
    assert unblocking(store, repo) == ("triaged", "blocked on `alpha`, resolved; ready at P2", True)
    assert run(capsys, "approve", "beta", *opts(repo, store))[0] == 0
    assert unblocking(store, repo) == ("stale", "blocked on `alpha`, which is ruled delete", False)


def test_blocked_without_a_priority_on_a_pruned_blocker_reads_ruled_delete(
        repo: Path, store: Store) -> None:
    store.save("gamma", blocked_p2(repo, priority=None))
    alpha_hash = hash_of(repo, "alpha")
    store.save("alpha", ruled("delete", entry_hash=alpha_hash))
    write(repo / "TODO.md", without(TODO, "`alpha`"))
    assert unblocking(store, repo) == ("stale", "blocked on `alpha`, which is resolved", False)
    store.save("alpha", approved("delete", entry_hash=alpha_hash))
    assert unblocking(store, repo) == ("stale", "blocked on `alpha`, which is ruled delete", False)


def test_ready_and_attempt_take_an_unblocked_entry(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gamma", blocked_p2(repo))
    root = tmp_path / "adr"
    assert run(capsys, "ready", *opts(repo, store))[0] == 3
    code, _, err = run(capsys, "attempt", "--selection", str(selection(store, ["gamma"])),
                       "--adr-root", str(root), *opts(repo, store))
    assert code == 1 and ("`gamma` is not ready: blocked on `alpha` (open), `beta` (open), "
                          "neither resolved nor in this selection") in err
    write(repo / "TODO.md", without(TODO, "`alpha`"))
    assert run(capsys, "ready", *opts(repo, store))[0] == 3
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    code, out, _ = run(capsys, "ready", *opts(repo, store))
    assert code == 0 and out.splitlines()[0].split("\t")[:4] == [
        "gamma", "P2", "triaged", "alpha,beta"]
    code, out, _ = run(capsys, "ready", "--json", *opts(repo, store))
    (item,) = json.loads(out)
    assert (item["slug"], item["priority"], item["disposition"], item["unblocked"],
            item["blocked_on"]) == ("gamma", 2, "blocked", True, ["alpha", "beta"])
    assert run(capsys, "ready", "--priority", "2", *opts(repo, store))[0] == 0
    assert run(capsys, "ready", "--priority", "3", *opts(repo, store))[0] == 3
    assert run(capsys, "ready", "--max-priority", "2", *opts(repo, store))[0] == 0
    assert run(capsys, "ready", "--max-priority", "1", *opts(repo, store))[0] == 3
    code, out, _ = run(capsys, "list", "--json", *opts(repo, store))
    assert {r["slug"]: r for r in json.loads(out)}["gamma"]["unblocked"] is True
    code, out, err = run(capsys, "attempt", "--selection", str(selection(store, ["gamma"])),
                         "--adr-root", str(root), *opts(repo, store))
    assert code == 0, err
    assert store.load("gamma")["attempts"][0]["slugs"] == ["gamma"]


def test_ready_ranks_an_unblocked_entry_among_do_entries(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta")
          + "\n## `delta`\n\nLater work.\n\n## `epsilon`\n\nUrgent work.\n")
    store.save("gamma", blocked_p2(repo))
    store.save("delta", approved(entry_hash=hash_of(repo, "delta"), priority=3))
    store.save("epsilon", approved(entry_hash=hash_of(repo, "epsilon"), priority=1))
    code, out, _ = run(capsys, "ready", *opts(repo, store))
    assert code == 0 and [line.split("\t")[0] for line in out.splitlines()] == [
        "epsilon", "gamma", "delta"]


def test_ready_skips_a_pending_unblocked_entry(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gamma", blocked_p2(repo, review=None))
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    assert run(capsys, "ready", *opts(repo, store))[0] == 3
    code, out, _ = run(capsys, "inbox", *opts(repo, store))
    assert code == 0 and out.splitlines()[0] == (
        f"gamma\ttriaged\tblocked P2\t-\t{dt.date.today().isoformat()}\ts")


def test_next_skips_an_unblocked_entry_unless_stale(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    store.save("gamma", blocked_p2(repo))
    assert run(capsys, "next", *opts(repo, store))[0] == 3
    store.save("gamma", blocked_p2(repo, date=dt.date.today() - dt.timedelta(days=400)))
    code, out, _ = run(capsys, "next", *opts(repo, store))
    assert code == 0 and lines_of(out)["slug"] == "gamma"
    assert lines_of(out)["status"] == "stale"


def test_summary_lists_unblocked_entries(repo: Path, store: Store,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    store.save("gamma", blocked_p2(repo))
    write(repo / "TODO.md", without(TODO, "`alpha`", "beta"))
    _, out, _ = run(capsys, "summary", *opts(repo, store))
    text = Path(out.strip()).read_text(encoding="utf-8")
    assert "- blocked P2: 1 settled, 0 pending" in text
    assert ("## Unblocked\n\n- `gamma`: blocked on `alpha`, `beta`, resolved; ready at P2\n\n"
            "## Open passes") in text
    store.save("gamma", blocked_p2(repo, date=dt.date.today() - dt.timedelta(days=400)))
    _, out, _ = run(capsys, "summary", *opts(repo, store))
    text = Path(out.strip()).read_text(encoding="utf-8")
    assert ("- `gamma`: blocked on `alpha`, `beta`, resolved; ready at P2 (also stale: ruled "
            in text)


CHAINS = TODO + "\n## `delta`\n\nThird link.\n\n## `epsilon`\n\nAnother root.\n"


def blocked_after(repo: Path, slug: str, deps: list[str], priority: int | None = 2,
                  review: str | None = "approved", **kwargs: Any) -> dict[str, Any]:
    """`slug` ruled blocked on `deps`, every one of them an entry at ruling time."""
    fields: dict[str, Any] = {"priority": priority, "blocked_on": deps,
                              "blocked_on_entries": deps,
                              "entry_hash": hash_of(repo, slug), **kwargs}
    return ruled("blocked", review=review, **fields)


def chain_setup(repo: Path, store: Store, rulings: dict[str, Any]) -> None:
    """Write `CHAINS`; an int ruling is an approved `do` at that priority, a
    (priority, deps) pair an approved `blocked` with that priority."""
    write(repo / "TODO.md", CHAINS)
    for slug, spec in rulings.items():
        if isinstance(spec, int):
            store.save(slug, approved(priority=spec, entry_hash=hash_of(repo, slug)))
        else:
            store.save(slug, blocked_after(repo, slug, spec[1], priority=spec[0]))


def chains(repo: Path, store: Store, priority: int | None = None,
           max_priority: int | None = None, slugs: list[str] | None = None,
           exclude: list[str] | None = None) -> dict[str, Any]:
    """`ops.followers` as nested `(slug, [...])` lists, keyed by ready slug."""
    all_rows = rows(store, todomd.read(repo, "TODO.md"))
    scope = ops.Scope(priority, max_priority, None if slugs is None else frozenset(slugs),
                      frozenset(exclude or ()))
    found = ops.followers(all_rows, ops.ready(all_rows, scope), scope)

    def plain(unlocks: tuple[ops.Follower, ...]) -> list[Any]:
        return [(f.row.slug, plain(f.unlocks)) for f in unlocks]

    return {slug: plain(unlocks) for slug, unlocks in found.items()}


def test_rows_carry_blocker_states(repo: Path, store: Store) -> None:
    store.save("alpha", approved(entry_hash=hash_of(repo, "alpha")))
    store.save("gamma", blocked_p2(repo))
    write(repo / "TODO.md", without(TODO, "beta"))
    by_slug = {r.slug: r for r in rows(store, todomd.read(repo, "TODO.md"))}
    assert by_slug["gamma"].blockers == (("alpha", BlockerState.OPEN),
                                         ("beta", BlockerState.CLEARED))
    assert by_slug["alpha"].blockers == ()


def test_followers_go_two_levels_deep(repo: Path, store: Store) -> None:
    chain_setup(repo, store, {"alpha": 2, "beta": (2, ["alpha"]), "gamma": (1, ["beta"]),
                              "delta": (3, ["gamma"])})
    assert chains(repo, store) == {"alpha": [("beta", [("gamma", [])])]}
    all_rows = rows(store, todomd.read(repo, "TODO.md"))
    found = ops.followers(all_rows, ops.ready(all_rows, ops.Scope()), ops.Scope())
    (beta,) = found["alpha"]
    (gamma,) = beta.unlocks
    assert beta.row.slug == "beta" and gamma.row.slug == "gamma" and gamma.unlocks == ()


def test_a_follower_sits_at_the_shallowest_level_covering_its_blockers(
        repo: Path, store: Store) -> None:
    chain_setup(repo, store, {"alpha": 2, "beta": (2, ["alpha"]),
                              "gamma": (2, ["alpha", "beta"])})
    assert chains(repo, store) == {"alpha": [("beta", [("gamma", [])])]}


def test_a_follower_of_two_ready_entries_is_under_both_ranked_by_priority(
        repo: Path, store: Store) -> None:
    chain_setup(repo, store, {"alpha": 2, "epsilon": 3, "beta": (3, ["alpha"]),
                              "gamma": (1, ["alpha", "epsilon"]), "delta": (3, ["alpha"])})
    assert chains(repo, store) == {"alpha": [("gamma", []), ("beta", []), ("delta", [])],
                                   "epsilon": [("gamma", [])]}


def test_a_level_two_follower_blocked_on_another_ready_entry_is_not_offered(
        repo: Path, store: Store) -> None:
    chain_setup(repo, store, {"alpha": 2, "epsilon": 3, "beta": (2, ["alpha"]),
                              "gamma": (1, ["epsilon", "beta"])})
    assert chains(repo, store) == {"alpha": [("beta", [])], "epsilon": []}


def test_a_level_two_follower_of_two_roots_is_under_both(repo: Path, store: Store) -> None:
    chain_setup(repo, store, {"alpha": 2, "epsilon": 3, "beta": (2, ["alpha"]),
                              "delta": (2, ["epsilon"]), "gamma": (1, ["beta", "delta"])})
    assert chains(repo, store) == {"alpha": [("beta", [("gamma", [])])],
                                   "epsilon": [("delta", [("gamma", [])])]}
    store.save("gamma", blocked_after(repo, "gamma", ["alpha", "delta"], priority=1))
    assert chains(repo, store) == {"alpha": [("beta", [])], "epsilon": [("delta", [])]}


def test_a_follower_needs_every_blocker_cleared_or_offered(repo: Path, store: Store) -> None:
    chain_setup(repo, store, {"alpha": 2, "gamma": (2, ["alpha", "beta"])})
    assert chains(repo, store) == {"alpha": []}
    write(repo / "TODO.md", without(CHAINS, "beta"))
    assert chains(repo, store) == {"alpha": [("gamma", [])]}
    store.save("beta", approved("delete", entry_hash=""))
    assert chains(repo, store) == {"alpha": []}


def test_followers_must_be_eligible_and_in_scope(repo: Path, store: Store) -> None:
    chain_setup(repo, store, {"alpha": 2, "beta": (2, ["alpha"])})
    assert chains(repo, store) == {"alpha": [("beta", [])]}
    variants: dict[str, dict[str, Any]] = {
        "no priority": blocked_after(repo, "beta", ["alpha"], priority=None),
        "pending": blocked_after(repo, "beta", ["alpha"], review=None),
        "modified": blocked_after(repo, "beta", ["alpha"], entry_hash="0" * 64),
    }
    with_attempt = blocked_after(repo, "beta", ["alpha"])
    with_attempt["attempts"] = [{"adr_dir": "/x", "started": "2026-09-01T00:00:00",
                                 "closed": None}]
    variants["open attempt"] = with_attempt
    for name, data in variants.items():
        store.save("beta", data)
        assert chains(repo, store) == {"alpha": []}, name
    store.save("beta", blocked_after(repo, "beta", ["alpha"], priority=3))
    assert chains(repo, store) == {"alpha": [("beta", [])]}
    assert chains(repo, store, max_priority=2) == {"alpha": []}
    assert chains(repo, store, exclude=["beta"]) == {"alpha": []}
    assert chains(repo, store, slugs=["alpha"]) == {"alpha": []}


def test_a_stale_follower_is_offered(repo: Path, store: Store) -> None:
    chain_setup(repo, store, {"alpha": 2})
    store.save("beta", blocked_after(repo, "beta", ["alpha"],
                                     date=dt.date.today() - dt.timedelta(days=400)))
    assert chains(repo, store) == {"alpha": [("beta", [])]}


def test_a_filtered_out_ready_entry_hides_its_followers(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    chain_setup(repo, store, {"alpha": 3, "beta": (1, ["alpha"])})
    assert chains(repo, store, priority=1) == {}
    code, out, _ = run(capsys, "ready", "--max-priority", "1", *opts(repo, store))
    assert code == 3 and out.strip() == "nothing ready"
    assert run(capsys, "ready", "--slugs", "beta", *opts(repo, store))[0] == 3


def test_a_blocker_cycle_is_not_offered(repo: Path, store: Store) -> None:
    chain_setup(repo, store, {"alpha": (2, ["beta"]), "beta": (2, ["alpha"])})
    assert chains(repo, store) == {}


def test_ready_shows_what_each_entry_unlocks(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    chain_setup(repo, store, {"alpha": 2, "epsilon": 3, "beta": (2, ["alpha"]),
                              "gamma": (1, ["beta"]), "delta": (3, ["alpha"])})
    code, out, _ = run(capsys, "ready", *opts(repo, store))
    columns = rows_by_slug(out)
    assert code == 0 and list(columns) == ["alpha", "epsilon"]
    assert columns["alpha"][-1] == "beta>gamma,delta"
    assert columns["epsilon"][-1] == "-" and len(columns["epsilon"]) == 9
    code, out, _ = run(capsys, "ready", "--json", *opts(repo, store))
    alpha, epsilon = json.loads(out)
    assert epsilon["unlocks"] == []
    beta, delta = alpha["unlocks"]
    assert list(beta) == ["slug", "priority", "status", "disposition", "unblocked",
                          "blocked_on", "first_seen", "cluster", "site_dirs", "attempts",
                          "unlocks"]
    assert (beta["slug"], beta["priority"], beta["status"], beta["disposition"],
            beta["unblocked"], beta["blocked_on"]) == ("beta", 2, "triaged", "blocked",
                                                       False, ["alpha"])
    assert beta["first_seen"] == "2026-01-02" and beta["site_dirs"] == ["src/core"]
    (gamma,) = beta["unlocks"]
    assert gamma["slug"] == "gamma" and gamma["unlocks"] == []
    assert delta["slug"] == "delta" and delta["unlocks"] == []
    code, out, _ = run(capsys, "ready", "--max-priority", "2", *opts(repo, store))
    assert out.splitlines() and out.splitlines()[0].split("\t")[-1] == "beta>gamma"


def test_ready_looks_up_a_shared_follower_once(
        repo: Path, store: Store, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    chain_setup(repo, store, {"alpha": 2, "epsilon": 3, "gamma": (1, ["alpha", "epsilon"]),
                              "delta": (2, ["gamma"])})
    looked_up: list[str] = []
    real = todomd.first_seen

    def counting(repo_: Path, path: str, heading: str) -> str | None:
        looked_up.append(heading)
        return real(repo_, path, heading)

    monkeypatch.setattr(todomd, "first_seen", counting)
    code, out, _ = run(capsys, "ready", *opts(repo, store))
    assert code == 0 and [line.split("\t")[-1] for line in out.splitlines()] == [
        "gamma>delta", "gamma>delta"]
    assert len(looked_up) == len(set(looked_up)) == 4


def attempt_on(capsys: pytest.CaptureFixture[str], repo: Path, store: Store, root: Path,
               slugs: list[str]) -> tuple[int, str, str]:
    return run(capsys, "attempt", "--selection", str(selection(store, slugs)),
               "--adr-root", str(root), *opts(repo, store))


def test_attempt_takes_a_selection_closed_over_its_blockers(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "adr"
    chain_setup(repo, store, {"alpha": 2, "beta": (2, ["alpha"]), "gamma": (1, ["beta"])})
    code, _, err = attempt_on(capsys, repo, store, root, ["beta"])
    assert code == 1 and ("`beta` is not ready: blocked on `alpha` (open), neither resolved "
                          "nor in this selection") in err
    code, _, err = attempt_on(capsys, repo, store, root, ["gamma", "alpha"])
    assert code == 1 and "`gamma` is not ready" in err
    code, out, err = attempt_on(capsys, repo, store, root, ["gamma", "beta", "alpha"])
    assert code == 0, err
    for slug in ("alpha", "beta", "gamma"):
        assert store.load(slug)["attempts"][0]["slugs"] == ["gamma", "beta", "alpha"]


def test_attempt_rejects_a_follower_whose_other_blocker_is_outside(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "adr"
    chain_setup(repo, store, {"alpha": 2, "beta": 3, "gamma": (2, ["alpha", "beta"])})
    code, _, err = attempt_on(capsys, repo, store, root, ["alpha", "gamma"])
    assert code == 1 and ("`gamma` is not ready: blocked on `beta` (open), neither resolved "
                          "nor in this selection") in err
    assert attempt_on(capsys, repo, store, root, ["alpha", "beta", "gamma"])[0] == 0


def test_attempt_names_a_deleted_blocker(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    chain_setup(repo, store, {"alpha": 2, "gamma": (2, ["alpha", "beta"])})
    store.save("beta", approved("delete", entry_hash=hash_of(repo, "beta")))
    code, _, err = attempt_on(capsys, repo, store, tmp_path / "adr", ["alpha", "gamma"])
    assert code == 1 and ("`gamma` is not ready: blocked on `beta` (deleted), neither "
                          "resolved nor in this selection") in err


@pytest.mark.parametrize("variant", [{"priority": None}, {"review": None}],
                         ids=["no priority", "pending"])
def test_attempt_rejects_an_ineligible_follower_in_a_closed_selection(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        variant: dict[str, Any]) -> None:
    chain_setup(repo, store, {"alpha": 2})
    store.save("beta", blocked_after(repo, "beta", ["alpha"], **variant))
    code, _, err = attempt_on(capsys, repo, store, tmp_path / "adr", ["alpha", "beta"])
    assert code == 1 and "`beta` is not ready" in err
    if variant == {"priority": None}:
        assert ("`beta` is not ready: triaged, ruled blocked, review approved; only standing, "
                "settled `do` rulings, or `blocked` rulings with a priority whose blockers are "
                "all resolved or in this selection, can be attempted") in err
    assert "attempts" not in (store.load("alpha") or {})


def test_attempt_on_a_level_two_follower_blocked_on_another_ready_entry(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "adr"
    chain_setup(repo, store, {"alpha": 2, "epsilon": 3, "beta": (2, ["alpha"]),
                              "gamma": (1, ["epsilon", "beta"])})
    code, _, err = attempt_on(capsys, repo, store, root, ["alpha", "beta", "gamma"])
    assert code == 1 and ("`gamma` is not ready: blocked on `epsilon` (open), neither "
                          "resolved nor in this selection") in err
    code, _, err = attempt_on(capsys, repo, store, root, ["alpha", "epsilon", "beta", "gamma"])
    assert code == 0, err


def test_attempt_on_a_level_two_follower_of_two_roots(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "adr"
    chain_setup(repo, store, {"alpha": 2, "epsilon": 3, "beta": (2, ["alpha"]),
                              "delta": (2, ["epsilon"]), "gamma": (1, ["beta", "delta"])})
    code, _, err = attempt_on(capsys, repo, store, root, ["alpha", "beta", "gamma"])
    assert code == 1 and ("`gamma` is not ready: blocked on `delta` (open), neither "
                          "resolved nor in this selection") in err
    code, _, err = attempt_on(capsys, repo, store, root,
                              ["alpha", "beta", "epsilon", "delta", "gamma"])
    assert code == 0, err


def test_attempt_takes_a_follower_whose_other_blocker_is_resolved(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    chain_setup(repo, store, {"alpha": 2, "gamma": (2, ["alpha", "beta"])})
    write(repo / "TODO.md", without(CHAINS, "beta"))
    code, _, err = attempt_on(capsys, repo, store, tmp_path / "adr", ["alpha", "gamma"])
    assert code == 0, err


def test_attempt_takes_a_blocker_cycle_whole(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "adr"
    chain_setup(repo, store, {"alpha": (2, ["beta"]), "beta": (2, ["alpha"])})
    code, _, err = attempt_on(capsys, repo, store, root, ["alpha"])
    assert code == 1 and "`alpha` is not ready" in err
    code, _, err = attempt_on(capsys, repo, store, root, ["alpha", "beta"])
    assert code == 0, err


def test_attempt_close_on_a_chain_unit(
        repo: Path, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "adr"
    chain_setup(repo, store, {"alpha": 2, "beta": (2, ["alpha"])})
    code, out, _ = attempt_on(capsys, repo, store, root, ["alpha", "beta"])
    assert code == 0
    adr = out.strip()
    write(repo / "TODO.md", without(CHAINS, "`alpha`"))
    code, out, _ = run(capsys, "attempt-close", "--adr", adr, *opts(repo, store))
    assert code == 0 and out.splitlines() == ["incomplete: alpha, beta",
                                              "still in TODO.md: beta"]
    chain_setup(repo, store, {"alpha": 2, "beta": (2, ["alpha"])})
    code, out, _ = attempt_on(capsys, repo, store, root, ["alpha", "beta"])
    assert code == 0
    write(repo / "TODO.md", without(CHAINS, "`alpha`", "beta"))
    code, out2, _ = run(capsys, "attempt-close", "--adr", out.strip(), *opts(repo, store))
    assert code == 0 and out2.splitlines() == ["shipped: alpha, beta"]

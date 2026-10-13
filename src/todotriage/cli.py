"""todo-triage: parse and lint TODO.md, keep the triage store, answer the workflows' queries.

Exit codes: 0 ok, 1 error, 3 nothing (an empty answer from `next`, `ready` or `inbox`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import TriageError
from . import ops, todomd
from .store import (
    DEFAULT_TODO_PATH,
    PRIORITIES,
    Disposition,
    MateStanding,
    Outcome,
    Review,
    Row,
    Status,
    Store,
    as_date,
    latest_ruled_pass,
    mate_standing,
    next_pass_ordinal,
    open_pass,
    rows,
    unblocked_reason,
)

EXIT_OK, EXIT_ERROR, EXIT_NOTHING = 0, 1, 3


@dataclass
class Output:
    """What a command says: text lines for agents, a JSON value for `--json`."""

    lines: list[str] = field(default_factory=list)
    data: Any = None
    code: int = EXIT_OK
    errors: list[str] = field(default_factory=list)  # printed to stderr in both modes


@dataclass
class Context:
    repo: Path
    store_path: Path | None

    def store(self) -> Store:
        if self.store_path is None:
            raise TriageError("--store is required for this command")
        return Store.open(self.store_path)

    def todo(self, store: Store | None) -> todomd.TodoFile:
        return todomd.read(self.repo, self.todo_path(store))

    def todo_path(self, store: Store | None) -> str:
        return store.config.todo_path if store else DEFAULT_TODO_PATH


def check_repo(repo: Path) -> Path:
    result = todomd.git(repo, "rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise TriageError(f"--repo {repo} is not a git work tree")
    return repo


def dash(value: Any) -> str:
    return "-" if value in (None, "", []) else str(value)


def row_json(row: Row) -> dict[str, Any]:
    return {
        "slug": row.slug,
        "status": row.status,
        "review": row.review,
        "reason": row.reason or None,
        "disposition": row.disposition,
        "priority": row.priority,
        "unblocked": row.unblocked,
        "ruled": row.ruled_date.isoformat() if row.ruled_date else None,
        "cluster": row.cluster,
        "line": row.entry.line if row.entry else None,
    }


def row_line(row: Row) -> str:
    priority = f"P{row.priority}" if row.priority is not None else None
    cluster = ",".join(row.cluster) if row.cluster else None
    fields = [row.slug, row.status, row.review, dash(row.disposition), dash(priority),
              dash(row.ruled_date), dash(cluster)]
    return "\t".join(fields)


def cmd_init(ctx: Context, args: argparse.Namespace) -> Output:
    if ctx.store_path is None:
        raise TriageError("--store is required for this command")
    ctx.store_path.mkdir(parents=True, exist_ok=True)
    store = Store.init(ctx.store_path, args.tag, args.todo_path, args.stale_days, args.ignore)
    path = store.root / "store.yaml"
    return Output([str(path)], {"store": str(path)})


def lint_issues(ctx: Context, store: Store | None, todo: todomd.TodoFile,
                ) -> tuple[str, list[todomd.Issue], list[todomd.Site]]:
    """Parse issues plus code-site checks; returns (todo path, issues, sites)."""
    todo_path = ctx.todo_path(store)
    issues = list(todo.issues)
    ignore = set(store.config.ignore) if store else set()
    sites = todomd.code_sites(ctx.repo, todo_path)
    with_sites = {site.slug for site in sites}
    for entry in todo.entries:
        if entry.slug not in with_sites and entry.slug not in ignore:
            issues.append(todomd.Issue("warning", entry.line,
                                       f"`{entry.slug}` has no `TODO({entry.slug})` code site"))
    known = set(todo.by_slug())
    for site in sites:
        if site.slug not in known:
            issues.append(todomd.Issue(
                "warning", 0, f"{site.path}:{site.line}: `TODO({site.slug})` matches no entry"))
    return todo_path, issues, sites


def cmd_lint(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store() if ctx.store_path is not None else None
    todo_path, issues, _ = lint_issues(ctx, store, ctx.todo(store))
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    lines = [i.render(todo_path) for i in errors + warnings]
    lines.append(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    data = {"errors": [i.__dict__ for i in errors], "warnings": [i.__dict__ for i in warnings]}
    return Output(lines, data, EXIT_ERROR if errors else EXIT_OK)


def cmd_list(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    selected = rows(store, ctx.todo(store))
    statuses = set(args.status or ())
    if args.stale:
        statuses.add(Status.STALE)
    if statuses:
        selected = [r for r in selected if r.status in statuses]
    if args.disposition:
        selected = [r for r in selected if r.disposition in args.disposition]
    if args.priority is not None:
        selected = [r for r in selected if r.priority == args.priority]
    if args.review:
        selected = [r for r in selected if r.review in args.review]
    return Output([row_line(r) for r in selected], [row_json(r) for r in selected])


def find_row(all_rows: list[Row], slug: str, todo_path: str) -> Row:
    for row in all_rows:
        if row.slug == slug:
            return row
    raise TriageError(f"`{slug}` is neither in {todo_path} nor in the store")


def show_row(ctx: Context, store: Store, row: Row,
             all_sites: list[todomd.Site]) -> tuple[list[str], dict[str, Any]]:
    """`show`'s text lines and JSON payload for one row."""
    todo_path = store.config.todo_path
    sites = [s for s in all_sites if s.slug == row.slug]
    seen = todomd.first_seen(ctx.repo, todo_path, row.entry.heading) if row.entry else None
    triage_file = store.triage_path(row.slug)

    status = f"{row.status} ({row.reason})" if row.reason else row.status
    lines = [f"slug: {row.slug}", f"status: {status}", f"review: {row.review}",
             f"first seen: {seen or 'uncommitted'}"]
    if row.entry:
        lines += [f"entry ({todo_path}:{row.entry.line}):", row.entry.text]
    else:
        lines.append(f"entry: not in {todo_path}")
    lines.append("code sites:")
    lines += [f"  {s.render()}" for s in sites] or ["  (none)"]
    docs: list[dict[str, Any]] = []
    for record in (row.data or {}).get("passes") or []:
        for doc in record.get("docs") or []:
            docs.append({"pass": record.get("pass"), **doc})
    lines.append("documents:")
    lines += [f"  t{d['pass']} {d.get('step')}: {store.root / d['path']}"
              + (f" (rejected: {d['rejected']})" if d.get("rejected") else "")
              for d in docs] or ["  (none)"]
    if row.data is not None:
        lines += [f"triage.yaml ({triage_file}):", triage_file.read_text(encoding="utf-8").rstrip()]
    else:
        lines.append("triage.yaml: (none)")
    data = {
        **row_json(row),
        "first_seen": seen,
        "entry": row.entry.text if row.entry else None,
        "sites": [s.__dict__ for s in sites],
        "documents": [{**d, "path": str(store.root / d["path"])} for d in docs],
        "triage": row.data,
    }
    return lines, data


def cmd_show(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    row = find_row(rows(store, ctx.todo(store)), args.slug, store.config.todo_path)
    lines, data = show_row(ctx, store, row, todomd.code_sites(ctx.repo, store.config.todo_path))
    return Output(lines, data)


def ruling_label(ruling: dict[str, Any]) -> str:
    """`do P3`, `blocked`, …"""
    priority = f" P{ruling['priority']}" if ruling.get("priority") is not None else ""
    return f"{ruling.get('disposition')}{priority}"


def inbox_rows(all_rows: list[Row]) -> list[Row]:
    """Pending agent rulings on entries neither mid-pass nor gone, in file order."""
    return [r for r in all_rows if r.review == Review.PENDING
            and r.status not in (Status.OPEN, Status.RESOLVED, Status.IGNORED)]


def live_mates(row: Row, by_slug: dict[str, Row]) -> list[str]:
    """The mates in `row`'s cluster that still share its ruling (`MateStanding.LIVE`)."""
    return [s for s in row.cluster if s != row.slug
            and mate_standing(row.slug, by_slug[s].data if s in by_slug else None)
            == MateStanding.LIVE]


def inbox_line(row: Row, by_slug: dict[str, Row]) -> str:
    """Tab-separated: slug, status, ruling, live cluster mates, ruled date, summary."""
    mates = live_mates(row, by_slug)
    summary = (row.ruling or {}).get("summary") or "-"
    return "\t".join([row.slug, row.status, ruling_label(row.ruling or {}), dash(",".join(mates)),
                      dash(row.ruled_date), summary])


def absolute(store: Store, path: str | None) -> str | None:
    """`path` (store-relative) made absolute against the store root; None stays None."""
    return str(store.root / path) if path else None


def cmd_inbox(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    all_rows = rows(store, ctx.todo(store))
    by_slug = {r.slug: r for r in all_rows}
    pending = inbox_rows(all_rows)
    if not pending:
        return Output(["no rulings awaiting review"], [], EXIT_NOTHING)
    lines = [inbox_line(r, by_slug) for r in pending]
    lines.append(f"{len(pending)} ruling(s) awaiting review")
    data = []
    if args.json:
        all_sites = todomd.code_sites(ctx.repo, store.config.todo_path)
        for row in pending:
            _, payload = show_row(ctx, store, row, all_sites)
            docs = ops.ruled_pass_docs(row)
            payload.update({step: absolute(store, path) for step, path in docs.items()})
            mates = live_mates(row, by_slug)
            payload["mates"] = mates
            explorations, previous = ops.cluster_pass_history(row, [by_slug[s] for s in mates])
            payload["explorations"] = [absolute(store, path) for path in explorations]
            payload["previous"] = [
                {"slug": p.slug, "pass": p.ordinal, "ruling": absolute(store, p.ruling),
                 "ruled_by": p.ruled_by, "review": absolute(store, p.review), "state": p.state}
                for p in previous]
            data.append(payload)
    return Output(lines, data)


def read_note(args: argparse.Namespace) -> str | None:
    """The text of `--note` or `--notes-file` (`-` is stdin); None when neither was given."""
    if args.note is not None:
        return args.note
    if args.notes_file is None:
        return None
    if args.notes_file == "-":
        return sys.stdin.read()
    return Path(args.notes_file).read_text(encoding="utf-8")


def review_output(path: Path | None, reviewed: list[ops.Reviewed],
                  state: Review) -> Output:
    lines = []
    for item in reviewed:
        if item.ruling is None:
            lines.append(f"{item.slug}: no ruling in store; not changed")
        elif item.open_ordinal is not None:
            lines.append(f"{item.slug}: pass {item.open_ordinal} is open; not changed")
        elif not item.changed:
            lines.append(f"{item.slug}: ruled separately since {item.ruling.get('date')}; "
                         "not changed")
        elif state == Review.APPROVED:
            lines.append(f"{item.slug}: approved ({ruling_label(item.ruling)}) "
                         f"{ops.today().isoformat()}")
        else:
            lines.append(f"{item.slug}: re-triage requested; next pass {item.next_pass}")
    data = {"state": state.value, "notes": str(path) if path else None,
            "changed": [i.slug for i in reviewed if i.changed],
            "unchanged": [i.slug for i in reviewed if not i.changed]}
    return Output(lines, data)


def cmd_review(ctx: Context, args: argparse.Namespace) -> Output:
    """`approve` and `retriage`; the subcommand sets `review_state`."""
    store = ctx.store()
    path, reviewed = ops.review(store, ctx.todo(store), args.slug, args.review_state,
                                read_note(args))
    return review_output(path, reviewed, args.review_state)


def cmd_sites(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    sites = [s for s in todomd.code_sites(ctx.repo, store.config.todo_path) if s.slug == args.slug]
    return Output([s.render() for s in sites], [s.__dict__ for s in sites])


PATH_TOKEN_RE = re.compile(r"[\w.-]*[\w-]/[\w./-]*[\w-]|\b[\w-]{2,}\.[a-z]{1,5}\b")


def path_tokens(text: str) -> set[str]:
    return set(PATH_TOKEN_RE.findall(text))


def mentions(row: Row, slug: str) -> bool:
    """Whether `row`'s entry body names `slug` in backticks."""
    return row.entry is not None and f"`{slug}`" in row.entry.body


def cmd_related(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    todo = ctx.todo(store)
    all_rows = rows(store, todo)
    target = find_row(all_rows, args.slug, store.config.todo_path)
    dirs = site_dirs(ctx, store)
    clustered: dict[str, set[str]] = {}
    for row in all_rows:
        for record in (row.data or {}).get("passes") or []:
            members = set((record.get("ruling") or {}).get("cluster") or [])
            for slug in members:
                clustered.setdefault(slug, set()).update(members - {slug})

    my_tokens = path_tokens(target.entry.body) if target.entry else set()
    my_dirs = set(dirs.get(target.slug, ()))
    my_cluster = clustered.get(target.slug, set())
    ranked: list[tuple[int, Row, list[str]]] = []
    for row in all_rows:
        if row.slug == target.slug or row.status == Status.IGNORED:
            continue
        reasons: list[str] = []
        score = 0
        if row.slug in my_cluster:
            score += 5
            reasons.append("prior cluster")
        shared_dirs = sorted(my_dirs.intersection(dirs.get(row.slug, ())))
        if shared_dirs:
            score += 2 * len(shared_dirs)
            reasons.append("site dirs: " + ", ".join(shared_dirs))
        if mentions(target, row.slug) or mentions(row, target.slug):
            score += 4
            reasons.append("mentions")
        shared_tokens = sorted(my_tokens & (path_tokens(row.entry.body) if row.entry else set()))
        if shared_tokens:
            score += len(shared_tokens)
            reasons.append("paths: " + ", ".join(shared_tokens))
        if score:
            ranked.append((score, row, reasons))
    ranked.sort(key=lambda item: -item[0])
    lines = [f"{row.slug}\t{score}\t{row.status}\t{'; '.join(reasons)}"
             for score, row, reasons in ranked] or ["(none)"]
    data = [{"slug": row.slug, "score": score, "status": row.status, "reasons": reasons}
            for score, row, reasons in ranked]
    return Output(lines, data)


def slug_args(values: list[str] | None) -> list[str] | None:
    """`a b`, `a,b` and `a, b` all name the same slugs; None when the option was not given."""
    if values is None:
        return None
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


# A re-triage request is offered only when `begin` can take the entry.
RETRIAGE_STATUSES = (Status.TRIAGED, Status.MODIFIED, Status.STALE)


def cmd_next(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    exclude = set(slug_args(args.exclude) or ())
    candidates = [r for r in rows(store, ctx.todo(store)) if r.slug not in exclude]
    choice: Row | None = next((r for r in candidates if r.review == Review.RETRIAGE
                               and r.status in RETRIAGE_STATUSES), None)
    if choice is None:
        choice = next((r for r in candidates if r.status == Status.UNTRIAGED), None)
    if choice is None:
        choice = next((r for r in candidates if r.status == Status.MODIFIED), None)
    if choice is None:
        stale = [r for r in candidates if r.status == Status.STALE]
        stale.sort(key=lambda r: r.ruled_date.toordinal() if r.ruled_date else 0)
        choice = stale[0] if stale else None
    if choice is None:
        return Output(["nothing to triage"], None, EXIT_NOTHING)
    ordinal = next_pass_ordinal(choice.data)
    lines = [f"slug: {choice.slug}", f"status: {choice.status}", f"pass: {ordinal}",
             f"review: {choice.review}"]
    return Output(lines, {"slug": choice.slug, "status": choice.status, "pass": ordinal,
                          "review": choice.review})


def cmd_begin(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    targets = ops.begin(store, ctx.todo(store), args.slug, args.resume)
    ruling = str(targets.ruling) if targets.ruling else "none (all ruling attempts used)"
    lines = [f"slug: {targets.slug}", f"pass: {targets.ordinal}",
             f"exploration: {targets.exploration}", f"sketch: {targets.sketch}",
             f"contrarian: {targets.contrarian}", f"ruling: {ruling}"]
    prior = [f"prior {d['step']}: {d['path']}" for d in targets.prior]
    if targets.prior_review is not None:
        prior.append(f"prior review ({targets.prior_review.get('state')}): "
                     f"{targets.prior_review.get('date')}")
    if targets.user_ruling is not None:
        prior.append(f"prior ruling (user): {ruling_text(targets.user_ruling)}")
    abandoned = [f"abandoned {d['step']}"
                 + (f" (rejected: {d['rejected']})" if d.get("rejected") else "")
                 + f": {d['path']}" for d in targets.abandoned]
    lines += prior or ["prior: (none)"]
    lines += abandoned or ["abandoned: (none)"]
    data = {"slug": targets.slug, "pass": targets.ordinal, "resumed": targets.resumed,
            "exploration": targets.exploration, "sketch": str(targets.sketch),
            "contrarian": str(targets.contrarian),
            "ruling": str(targets.ruling) if targets.ruling else None,
            "prior": targets.prior, "abandoned": targets.abandoned,
            "user_ruling": targets.user_ruling,
            "prior_review": ({"state": targets.prior_review.get("state"),
                              "date": targets.prior_review.get("date")}
                             if targets.prior_review is not None else None)}
    return Output(lines, data)


def ruling_text(ruling: dict[str, Any]) -> str:
    """`<disposition>[ P<n>][ blocked_on a,b][ defer_until <text>] <date>: <summary or ->`."""
    text = ruling_label(ruling)
    if ruling.get("blocked_on"):
        text += f" blocked_on {','.join(str(b) for b in ruling['blocked_on'])}"
    if ruling.get("defer_until"):
        text += f" defer_until {ruling['defer_until']}"
    return f"{text} {ruling.get('date')}: {ruling.get('summary') or '-'}"


def cmd_abandon(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    slugs = slug_args(args.slugs) or []
    if not slugs:
        raise TriageError("name at least one slug")
    ended = ops.abandon(store, slugs)
    return Output([f"{slug}: pass {t} abandoned" for slug, t in ended],
                  [{"slug": slug, "pass": t} for slug, t in ended])


def cmd_path(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    if args.selection:
        if args.slug or args.step:
            raise TriageError("--selection takes no SLUG or --step")
        path = ops.selection_path(store)
    else:
        if not args.slug or not args.step:
            raise TriageError("give SLUG --step ruling|notes, or --selection")
        path = (ops.next_ruling_path if args.step == "ruling" else ops.next_notes_path)(
            store, args.slug)
    return Output([str(path)], {"path": str(path)})


def cmd_log(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    if args.scan == (args.doc is not None):
        raise TriageError("give exactly one of --doc PATH or --scan")
    logged = ops.log_docs(store, ctx.todo(store), args.slug, args.step, args.doc, args.scan)
    lines = []
    for item in logged:
        lines.append(f"{args.step}: {store.root / item.path} -> "
                     f"{', '.join(item.slugs) or '(already registered)'}")
        lines += [f"held {slug}: {settled.describe()} ({settled.ruling.get('disposition')}); "
                  f"no pass opened — `todo-triage override {slug}` changes it"
                  for slug, settled in item.held]
        if args.step == "sketch" and not item.held:
            lines.append("held: (none)")
        lines += [f"review {slug} (retriage): {store.root / path}" for slug, path in item.reviews]
    lines = lines or [f"no unregistered {args.step} documents"]
    data = [{"path": str(store.root / item.path), "slugs": item.slugs,
             "held": [slug for slug, _ in item.held],
             "reviews": [{"slug": slug, "path": str(store.root / path)}
                         for slug, path in item.reviews]} for item in logged]
    return Output(lines, data)


def cmd_record(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    ruled, errors = ops.record_ruling(store, ctx.todo(store), args.slug, args.ruling)
    if errors:
        return Output([], {"recorded": [], "errors": errors}, EXIT_ERROR, errors)
    return Output([f"recorded: {', '.join(ruled)}"], {"recorded": ruled, "errors": []})


def cmd_override(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    fields = {"disposition": args.disposition, "priority": args.priority,
              "blocked_on": args.blocked_on or None, "defer_until": args.defer_until,
              "summary": args.summary}
    ruling = ops.override(store, ctx.todo(store), args.slug, fields)
    return Output([f"{args.slug}: {ruling['disposition']} (ruled_by: user)"], ruling)


def site_dirs(ctx: Context, store: Store) -> dict[str, list[str]]:
    dirs: dict[str, set[str]] = {}
    for site in todomd.code_sites(ctx.repo, store.config.todo_path):
        dirs.setdefault(site.slug, set()).add(str(PurePosixPath(site.path).parent))
    return {slug: sorted(d) for slug, d in dirs.items()}


def cmd_ready(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    todo = ctx.todo(store)
    chosen = ops.ready(rows(store, todo), args.priority, args.max_priority,
                       slug_args(args.slugs), slug_args(args.exclude))
    if not chosen:
        return Output(["nothing ready"], [], EXIT_NOTHING)
    todo_path = store.config.todo_path
    seen = {r.slug: todomd.first_seen(ctx.repo, todo_path, r.entry.heading)
            for r in chosen if r.entry}
    chosen.sort(key=lambda r: (r.priority or PRIORITIES[-1] + 1, seen.get(r.slug) or "9999"))
    dirs = site_dirs(ctx, store)
    lines, data = [], []
    for row in chosen:
        attempts = list((row.data or {}).get("attempts") or [])
        history = ",".join(f"{a.get('outcome')}@{str(a.get('started'))[:10]}" for a in attempts)
        lines.append("\t".join([row.slug, f"P{row.priority}", row.status,
                                dash(",".join(row.blocked_on)), dash(seen.get(row.slug)), dash(",".join(row.cluster)),
                                dash(",".join(dirs.get(row.slug, []))), dash(history)]))
        data.append({"slug": row.slug, "priority": row.priority, "status": row.status,
                     "disposition": row.disposition, "unblocked": row.unblocked,
                     "blocked_on": row.blocked_on,
                     "first_seen": seen.get(row.slug), "cluster": row.cluster,
                     "site_dirs": dirs.get(row.slug, []), "attempts": attempts})
    return Output(lines, data)


def cmd_attempt(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    adr = ops.start_attempt(store, ctx.todo(store), args.selection, args.adr_root)
    return Output([str(adr)], {"adr_dir": str(adr)})


def cmd_attempt_close(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    outcome, slugs = ops.close_attempt(store, ctx.todo(store), args.adr, args.outcome)
    return Output([f"{outcome}: {', '.join(slugs)}"], {"outcome": outcome, "slugs": slugs})


def cmd_prune(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    todo = ctx.todo(store)
    doomed = ops.prunable(rows(store, todo))
    todo_path = store.config.todo_path
    sites = todomd.code_sites(ctx.repo, todo_path)
    orphaned = [s for s in sites if s.slug in {r.slug for r in doomed}]
    lines = [f"{todo_path}:{r.entry.line}: `{r.slug}`" for r in doomed if r.entry]
    if not doomed:
        return Output(["no entries ruled delete"], {"pruned": [], "sites": []})
    if args.write:
        if any(i.level == "error" for i in todo.issues):
            raise TriageError(f"{todo_path} has lint errors; fix them before `prune --write`")
        path = ctx.repo / todo_path
        text = path.read_text(encoding="utf-8")
        path.write_text(ops.prune_text(text, {r.entry.line for r in doomed if r.entry}),
                        encoding="utf-8")
        ops.mark_pruned(store, [r.slug for r in doomed])
        lines = ["removed " + line for line in lines]
        lines.append("code sites to remove:" if orphaned else "no code sites to remove")
    else:
        lines.append("code sites:" if orphaned else "no code sites")
    lines += [f"  {s.render()}" for s in orphaned]
    data = {"pruned" if args.write else "prunable": [r.slug for r in doomed],
            "sites": [s.__dict__ for s in orphaned]}
    return Output(lines, data)


SUMMARY_RE = re.compile(r"^summary-(\d{8}-\d{6})(?:-(\d+))?\.md$")


def previous_summary(store: Store) -> tuple[Path, dt.date] | None:
    found = []
    for path in (store.root / "sessions").glob("summary-*.md"):
        match = SUMMARY_RE.match(path.name)
        if match:
            found.append((match[1], int(match[2] or 1), path))
    if not found:
        return None
    stamp, _, path = max(found)
    return path, dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").date()


def bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["- (none)"]


def cmd_summary(ctx: Context, args: argparse.Namespace) -> Output:
    store = ctx.store()
    todo = ctx.todo(store)
    all_rows = rows(store, todo)
    by_slug = {r.slug: r for r in all_rows}
    todo_path, issues, sites = lint_issues(ctx, store, todo)
    previous = previous_summary(store)
    since = previous[1] if previous else None
    stamp = ops.file_stamp()

    by_status = {s: sum(1 for r in all_rows if r.status == s) for s in Status}
    by_disposition: dict[str, list[int]] = {}
    for row in all_rows:
        if row.disposition and row.status not in (Status.RESOLVED, Status.IGNORED):
            key = row.disposition + (f" P{row.priority}" if row.priority is not None else "")
            counts = by_disposition.setdefault(key, [0, 0])
            counts[0 if row.settled else 1] += 1
    retriage = []
    for row in all_rows:
        if row.review == Review.RETRIAGE:
            review = (latest_ruled_pass(row.data) or {}).get("review") or {}
            notes = ops.ruled_pass_docs(row, ("review",))["review"]
            retriage.append(f"`{row.slug}`: {review.get('date')}, "
                            f"notes {store.root / notes if notes else '(none)'}")
    rulings = []
    for slug, ruling in ops.ruling_rows(all_rows):
        date = as_date(ruling.get("date"))
        if since is None or (date is not None and date >= since):
            priority = f" P{ruling['priority']}" if ruling.get("priority") else ""
            rulings.append(f"`{slug}`: {ruling.get('disposition')}{priority}, {date}, "
                           f"by {ruling.get('ruled_by')}: {ruling.get('summary') or '-'}")
    attempts_open, attempts_closed = [], []
    seen_adrs: set[str] = set()
    for row in all_rows:
        for attempt in (row.data or {}).get("attempts") or []:
            adr = str(attempt.get("adr_dir"))
            if adr in seen_adrs:
                continue
            seen_adrs.add(adr)
            covered = ", ".join(attempt.get("slugs") or [row.slug])
            closed = as_date(attempt.get("closed"))
            if closed is None:
                attempts_open.append(f"{adr} ({covered}), started {attempt.get('started')}")
            elif since is None or closed >= since:
                attempts_closed.append(f"{adr} ({covered}): {attempt.get('outcome')}")
    abandoned = []
    for row in all_rows:
        for record in (row.data or {}).get("passes") or []:
            when = record.get("abandoned")
            day = as_date(when)
            if when and (since is None or (day is not None and day >= since)):
                t = int(record.get("pass", 0))
                used = ops.ruling_attempts_used(store, row.slug, t)
                abandoned.append(f"`{row.slug}`: pass {t}, abandoned {when}, "
                                 f"{used} ruling attempt(s) used")
    known = set(todo.by_slug())

    lines = [
        f"# TODO triage summary {stamp}", "",
        f"Store: {store.root} — uncommitted store changes are yours to commit.",
        f"Previous summary: {previous[0] if previous else '(none)'}", "",
        "## Status", "", *bullets([f"{s}: {n}" for s, n in by_status.items() if n]), "",
        "## Dispositions (entries in the file)", "",
        *bullets([f"{d}: {settled} settled, {pending} pending"
                  for d, (settled, pending) in sorted(by_disposition.items())]), "",
        "## Awaiting review", "",
        *bullets([inbox_line(r, by_slug) for r in inbox_rows(all_rows)]), "",
        "## Re-triage requested", "", *bullets(retriage), "",
        f"## Rulings since {since or 'the start'}", "", *bullets(rulings), "",
        "## Stale", "",
        *bullets([f"`{r.slug}`: {r.reason}" for r in all_rows if r.status == Status.STALE]), "",
        "## Unblocked", "",
        *bullets([f"`{r.slug}`: {unblocked_reason(r.ruling or {})}"
                  + (f" (also stale: {r.reason})" if r.status == Status.STALE else "")
                  for r in all_rows if r.unblocked]), "",
        "## Open passes", "",
        *bullets([f"`{r.slug}`: pass {open_pass(r.data)['pass']}"  # type: ignore[index]
                  for r in all_rows if r.status == Status.OPEN]), "",
        f"## Abandoned passes since {since or 'the start'}", "", *bullets(abandoned), "",
        "## Open attempts", "", *bullets(attempts_open), "",
        f"## Attempts closed since {since or 'the start'}", "", *bullets(attempts_closed), "",
        "## Resolved (in the store, gone from the file)", "",
        *bullets([f"`{r.slug}`" for r in all_rows if r.status == Status.RESOLVED]), "",
        "## Code sites with no entry", "",
        *bullets([s.render() for s in sites if s.slug not in known]), "",
        "## Lint", "", *bullets([i.render(todo_path) for i in issues]), "",
    ]
    path = ops.unused(store.root / "sessions", f"summary-{stamp}", ".md")
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return Output([str(path)], {"summary": str(path)})


Command = Callable[[Context, argparse.Namespace], Output]


def add_global_options(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """`--repo`, `--store`, `--json`; given before the command or after it. With `suppress`
    an option not given sets nothing."""
    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress else value
    parser.add_argument("--repo", type=Path, default=default(None),
                        help="git work tree holding TODO.md (default: cwd)")
    parser.add_argument("--store", type=Path, default=default(None),
                        help="triage store directory (required for every command but lint)")
    parser.add_argument("--json", action="store_true", default=default(False),
                        help="machine-readable output")


def add_note_options(parser: argparse.ArgumentParser, required: bool) -> None:
    """Mutually exclusive `--note` and `--notes-file`."""
    notes = parser.add_mutually_exclusive_group(required=required)
    notes.add_argument("--note", metavar="TEXT", help="the user's words, verbatim")
    notes.add_argument("--notes-file", metavar="PATH",
                       help="read the user's words from PATH; `-` reads stdin")


def make_parser() -> argparse.ArgumentParser:
    # The subcommands' copies never overwrite a value given before the command.
    common = argparse.ArgumentParser(add_help=False)
    add_global_options(common, suppress=True)

    parser = argparse.ArgumentParser(prog="todo-triage", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    add_global_options(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: Command, help_text: str) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        cmd.set_defaults(func=func)
        return cmd

    init = add("init", cmd_init, "create store.yaml")
    init.add_argument("--tag", required=True, help="ADR tag; names burndown ADR dirs")
    init.add_argument("--todo-path", default=DEFAULT_TODO_PATH, help="TODO file relative to --repo")
    init.add_argument("--stale-days", type=int, default=180)
    init.add_argument("--ignore", nargs="*", default=[], metavar="SLUG")

    add("lint", cmd_lint, "check TODO.md structure, in-band markers and code sites")

    lst = add("list", cmd_list,
              "one line per entry: slug status review disposition priority ruled cluster")
    lst.add_argument("--status", nargs="+", choices=[s.value for s in Status])
    lst.add_argument("--disposition", nargs="+", choices=[d.value for d in Disposition])
    lst.add_argument("--priority", type=int, choices=PRIORITIES)
    lst.add_argument("--stale", action="store_true", help="shorthand for --status stale")
    lst.add_argument("--review", nargs="+", choices=[r.value for r in Review if r != Review.NONE])

    show = add("show", cmd_show, "entry text, code sites, first-seen, triage record")
    show.add_argument("slug")

    sites = add("sites", cmd_sites, "TODO(SLUG) comments outside the TODO file")
    sites.add_argument("slug")

    related = add("related", cmd_related, "other entries ranked by shared paths and clusters")
    related.add_argument("slug")

    nxt = add("next", cmd_next, "the next entry to triage; exit 3 when none")
    nxt.add_argument("--exclude", nargs="*", default=[], metavar="SLUG")

    begin = add("begin", cmd_begin, "open a triage pass; print its ordinal and target paths")
    begin.add_argument("slug")
    begin.add_argument("--resume", action="store_true", help="continue the open pass")

    abandon = add("abandon", cmd_abandon, "end each named slug's open pass without a ruling")
    abandon.add_argument("slugs", nargs="+", metavar="SLUG")

    path = add("path", cmd_path, "the next unused ruling, notes or selection path")
    path.add_argument("slug", nargs="?")
    path.add_argument("--step", choices=("ruling", "notes"))
    path.add_argument("--selection", action="store_true")

    log = add("log", cmd_log, "register a document in the open pass")
    log.add_argument("slug")
    log.add_argument("--step", required=True, choices=ops.DOC_STEPS)
    log.add_argument("--doc", type=Path)
    log.add_argument("--scan", action="store_true",
                     help="register every unregistered exploration of the open pass")

    record = add("record", cmd_record, "validate a ruling document and close the passes")
    record.add_argument("slug")
    record.add_argument("--ruling", required=True, type=Path)

    over = add("override", cmd_override, "record a human ruling, closing any open pass")
    over.add_argument("slug")
    over.add_argument("--disposition", required=True, choices=[d.value for d in Disposition])
    over.add_argument("--priority", type=int, choices=PRIORITIES)
    over.add_argument("--blocked-on", nargs="+")
    over.add_argument("--defer-until")
    over.add_argument("--summary")

    add("inbox", cmd_inbox, "agent rulings awaiting the user's review; exit 3 when none")

    approve = add("approve", cmd_review, "approve the agent ruling of SLUG and its cluster")
    approve.set_defaults(review_state=Review.APPROVED)
    approve.add_argument("slug")
    add_note_options(approve, required=False)

    retriage = add("retriage", cmd_review, "send SLUG and its cluster back for re-triage")
    retriage.set_defaults(review_state=Review.RETRIAGE)
    retriage.add_argument("slug")
    add_note_options(retriage, required=True)

    rdy = add("ready", cmd_ready,
              "settled `do` entries, and `blocked` entries with a priority whose blockers have"
              " all left TODO.md, ranked by priority then age, one line per entry: slug"
              " priority status blocked-on first-seen cluster site-dirs"
              " attempts(outcome@date,...); exit 3 when none")
    rdy.add_argument("--priority", type=int, choices=PRIORITIES, help="exactly P")
    rdy.add_argument("--max-priority", type=int, choices=PRIORITIES, help="P or better")
    rdy.add_argument("--slugs", nargs="+", metavar="SLUG")
    rdy.add_argument("--exclude", nargs="*", default=[], metavar="SLUG")

    att = add("attempt", cmd_attempt, "open a burndown attempt from a selection document")
    att.add_argument("--selection", required=True, type=Path)
    att.add_argument("--adr-root", required=True, type=Path)

    close = add("attempt-close", cmd_attempt_close, "close the open attempt for an ADR dir")
    close.add_argument("--adr", required=True, type=Path)
    close.add_argument("--outcome", choices=(Outcome.STOPPED.value,))

    prune = add("prune", cmd_prune, "list entries ruled delete; --write removes them")
    prune.add_argument("--write", action="store_true")

    add("summary", cmd_summary, "write a session summary; print its path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        repo = check_repo((args.repo or Path.cwd()).resolve())
        ctx = Context(repo=repo, store_path=args.store.resolve() if args.store else None)
        out = args.func(ctx, args)
    except TriageError as exc:
        print(f"todo-triage: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        print(f"todo-triage: {exc.strerror or exc}{where}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # a bug: one line for the drivers that relay stderr
        print(f"todo-triage: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    for line in out.errors:
        print(line, file=sys.stderr)
    if args.json:
        print(json.dumps(out.data, indent=2, default=str))
    else:
        for line in out.lines:
            print(line)
    return out.code


if __name__ == "__main__":
    sys.exit(main())

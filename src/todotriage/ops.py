"""Store mutations: triage passes, rulings, burndown attempts, pruning."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import TriageError
from .store import (
    PRIORITIES,
    BlockerState,
    Disposition,
    MateStanding,
    Outcome,
    Review,
    Row,
    RuledBy,
    Status,
    Store,
    latest_ruled_pass,
    next_pass_ordinal,
    latest_ruling,
    mate_standing,
    now_stamp,
    open_attempts,
    open_pass,
    is_settled,
    review_notes,
    review_state,
    rows,
    today,
    user_ruled,
)
from .todomd import ENTRY_START_RE, Site, TodoFile

MAX_RULING_ATTEMPTS = 3
DOC_STEPS = ("exploration", "sketch", "contrarian")
DISCLAIMER = (
    "> Written by the burndown selector, a product-owner proxy, from the TODO triage "
    "store; not the user's own words."
)

RULING_RE = re.compile(r"^ruling-t(\d+)-a(\d+)\.md$")
EXPLORATION_RE = re.compile(r"^exploration-t(\d+)-(\d+)\.md$")


def file_stamp(when: dt.datetime | None = None) -> str:
    return (when or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")


def unused(directory: Path, stem: str, suffix: str = "") -> Path:
    """`directory/stem+suffix`, or the first free `stem-2`, `stem-3`… variant."""
    candidate = directory / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{n}{suffix}"
        n += 1
    return candidate


class FrontmatterLoader(yaml.SafeLoader):
    """Safe YAML where a bare scalar is a string unless it is null or a plain decimal.

    Slugs such as `1234`, `on` or `2026-01-02` then survive as the text the agent
    wrote, instead of turning into an int that no longer matches, a bool or a date.
    """


FrontmatterLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers
            if tag in ("tag:yaml.org,2002:null", "tag:yaml.org,2002:merge")]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
FrontmatterLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int", re.compile(r"^(?:0|[-+]?[1-9][0-9]*)$"), list("-+0123456789"))


def slug_text(value: Any) -> Any:
    """A plain-decimal slug loads as an int; give it back its text."""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


@dataclass(frozen=True)
class Document:
    meta: dict[str, Any]
    body: str
    meta_lines: list[str]  # the frontmatter text, one item per line
    meta_start: int  # 1-based file line of the first frontmatter line


def read_document(path: Path) -> Document:
    """Split `---`-fenced YAML frontmatter from the body; no frontmatter → empty meta."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TriageError(f"{path} does not exist") from None
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return Document({}, text, [], 0)
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        raise TriageError(f"{path}:1: frontmatter opened with `---` is never closed")
    meta_lines = lines[1:close]
    try:
        meta = yaml.load("\n".join(meta_lines), Loader=FrontmatterLoader) or {}
    except yaml.YAMLError as exc:
        raise TriageError(f"{path}: invalid frontmatter YAML: {exc}") from None
    if not isinstance(meta, dict):
        raise TriageError(f"{path}:2: frontmatter must be a mapping")
    return Document(meta, "\n".join(lines[close + 1:]), meta_lines, 2)


def slug_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, list):
        value = [value]
    value = [slug_text(s) for s in value]
    if not value or not all(isinstance(s, str) and s for s in value):
        raise TriageError(f"{what} must be a non-empty list of slugs")
    return list(dict.fromkeys(value))


def in_store(store: Store, path: Path) -> str:
    resolved = path.resolve()
    try:
        return store.relative(resolved)
    except ValueError:
        raise TriageError(f"{path} is not inside the store {store.root}") from None


def require_entry(todo: TodoFile, store: Store, slug: str) -> None:
    if slug not in todo.by_slug():
        raise TriageError(f"`{slug}` is not an entry in {store.config.todo_path}")
    if slug in store.config.ignore:
        raise TriageError(f"`{slug}` is ignored in store.yaml")


def start_pass(store: Store, slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Append a new open pass to `slug`'s record; returns (record, pass)."""
    data = store.load(slug) or {"slug": slug}
    data.setdefault("slug", slug)
    record = {"pass": next_pass_ordinal(data), "started": now_stamp(), "docs": []}
    data.setdefault("passes", []).append(record)
    return data, record


def require_open(store: Store, slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    data = store.load(slug)
    record = open_pass(data)
    if data is None or record is None:
        raise TriageError(f"`{slug}` has no open pass; run `todo-triage begin {slug}`")
    return data, record


@dataclass(frozen=True)
class Settled:
    """A standing ruling the user ruled or approved; `review` is None for the user's own."""

    ruling: dict[str, Any]
    review: dict[str, Any] | None

    def describe(self) -> str:
        """`ruled by user <date>` or `approved by user <date>`."""
        return f"{self.verb()} by user {self.date()}"

    def sentence(self) -> str:
        """`ruled by the user (<date>)` or `approved by the user (<date>)`."""
        return f"{self.verb()} by the user ({self.date()})"

    def verb(self) -> str:
        return "ruled" if self.review is None else "approved"

    def date(self) -> Any:
        return (self.ruling if self.review is None else self.review).get("date")


@dataclass(frozen=True)
class Logged:
    """One document registered by `log_docs`."""

    path: str  # store-relative
    slugs: list[str]  # the slugs whose pass it was registered in by this call
    held: list[tuple[str, Settled]]  # covered slugs given no pass
    # (slug, store-relative notes path) for each covered mate the user sent back for re-triage
    reviews: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Targets:
    slug: str
    ordinal: int
    resumed: bool
    exploration: str
    sketch: Path
    contrarian: Path
    ruling: Path | None
    prior: list[dict[str, Any]]
    abandoned: list[dict[str, Any]]
    user_ruling: dict[str, Any] | None
    prior_review: dict[str, Any] | None


def begin(store: Store, todo: TodoFile, slug: str, resume: bool) -> Targets:
    require_entry(todo, store, slug)
    data = store.load(slug)
    current = open_pass(data)
    if current is not None and not resume:
        raise TriageError(f"`{slug}` already has open pass {current['pass']}; "
                          "use `begin --resume` to continue it")
    if current is None and resume:
        raise TriageError(f"`{slug}` has no open pass to resume")
    if current is None:
        data, current = start_pass(store, slug)
        store.save(slug, data)
    directory = store.entry_dir(slug)
    directory.mkdir(parents=True, exist_ok=True)
    t = int(current["pass"])
    earlier = [p for p in data.get("passes") or [] if int(p.get("pass", 0)) < t]
    prior_pass = ruled_pass_before(data, t)
    floor = int(prior_pass["pass"]) if prior_pass else 0
    abandoned_pass = next((p for p in reversed(earlier)
                           if p.get("abandoned") and int(p.get("pass", 0)) > floor), None)
    prior = [{**d, "path": str(store.root / d["path"])}
             for d in (prior_pass or {}).get("docs") or [] if not d.get("rejected")]
    abandoned = [{**d, "path": str(store.root / d["path"])}
                 for d in (abandoned_pass or {}).get("docs") or []]
    prior_ruling = (prior_pass or {}).get("ruling")
    user_ruling = prior_ruling if user_ruled(prior_ruling) else None
    prior_review = (prior_pass or {}).get("review") or None
    try:
        ruling: Path | None = next_ruling_path(store, slug)
    except TriageError:
        ruling = None
    return Targets(
        slug=slug, ordinal=t, resumed=resume,
        exploration=str(directory / f"exploration-t{t}-<N>.md"),
        sketch=directory / f"sketch-t{t}.md",
        contrarian=directory / f"contrarian-t{t}.md",
        ruling=ruling,
        prior=prior,
        abandoned=abandoned,
        user_ruling=user_ruling,
        prior_review=prior_review,
    )


def standing_settled(store: Store, slug: str) -> Settled | None:
    """`slug`'s standing ruling when the user ruled or approved it; a sketch cannot re-rule it."""
    data = store.load(slug)
    if not is_settled(data):
        return None
    record = latest_ruled_pass(data)
    assert record is not None
    ruling = record["ruling"]
    return Settled(ruling, None if user_ruled(ruling) else record["review"])


def attempts_on_disk(directory: Path, pattern: re.Pattern[str], t: int) -> list[int]:
    found = []
    for path in directory.glob("*.md") if directory.is_dir() else []:
        match = pattern.match(path.name)
        if match and int(match[1]) == t:
            found.append(int(match[2]))
    return sorted(found)


def ruling_attempts_used(store: Store, slug: str, t: int) -> int:
    """The number of ruling attempt files on disk for `slug`'s pass `t`."""
    return len(attempts_on_disk(store.entry_dir(slug), RULING_RE, t))


def next_ruling_path(store: Store, slug: str) -> Path:
    _, record = require_open(store, slug)
    t = int(record["pass"])
    directory = store.entry_dir(slug)
    a = max(attempts_on_disk(directory, RULING_RE, t), default=0) + 1
    if a > MAX_RULING_ATTEMPTS:
        raise TriageError(f"`{slug}` pass {t}: all {MAX_RULING_ATTEMPTS} ruling attempts used")
    return directory / f"ruling-t{t}-a{a}.md"


def next_notes_path(store: Store, slug: str) -> Path:
    """Notes answer the latest ruling attempt, so they share its `A`."""
    _, record = require_open(store, slug)
    t = int(record["pass"])
    directory = store.entry_dir(slug)
    rulings = attempts_on_disk(directory, RULING_RE, t)
    if not rulings:
        raise TriageError(f"`{slug}` pass {t}: no ruling attempt to answer yet")
    path = directory / f"notes-user-t{t}-a{rulings[-1]}.md"
    if path.exists():
        raise TriageError(f"{path} already exists; relaunch the ruling before more notes")
    return path


def session_path(store: Store, kind: str) -> Path:
    directory = store.root / "sessions"
    directory.mkdir(exist_ok=True)
    return unused(directory, f"{kind}-{file_stamp()}", ".md")


def selection_path(store: Store) -> Path:
    return session_path(store, "selection")


def prune_report_path(store: Store) -> Path:
    return session_path(store, "prune")


def registered(record: dict[str, Any]) -> set[str]:
    return {d["path"] for d in record.get("docs") or []}


def log_docs(store: Store, todo: TodoFile, slug: str, step: str, doc: Path | None,
             scan: bool) -> list[Logged]:
    """Register documents in the open pass and, for a sketch, its covered mates' passes.

    A covered mate whose standing ruling the user ruled or approved is held: no pass
    is opened for it and nothing is registered in one it holds. A covered mate the user
    sent back for re-triage is reported with its review notes, which this pass must answer.
    """
    data, record = require_open(store, slug)
    if scan:
        if step != "exploration":
            raise TriageError("--scan applies only to --step exploration")
        t = int(record["pass"])
        known = registered(record)
        paths = sorted(
            (p for p in store.entry_dir(slug).glob(f"exploration-t{t}-*.md")
             if EXPLORATION_RE.match(p.name)),
            key=lambda p: int(EXPLORATION_RE.match(p.name)[2]),  # type: ignore[index]
        )
        new = [store.relative(p) for p in paths if store.relative(p) not in known]
        for rel in new:
            record["docs"].append({"step": step, "path": rel, "date": today().isoformat()})
        store.save(slug, data)
        return [Logged(rel, [slug], []) for rel in new]
    if doc is None:
        raise TriageError("give --doc PATH or --scan")
    rel = in_store(store, doc)
    if not doc.is_file():
        raise TriageError(f"{doc} does not exist")
    entry: dict[str, Any] = {"step": step, "path": rel, "date": today().isoformat()}
    covers = [slug]
    held: list[tuple[str, Settled]] = []
    reviews: list[tuple[str, str]] = []
    if step == "sketch":
        meta = read_document(doc).meta
        if "covers" in meta:
            covers = slug_list(meta["covers"], f"{doc}: `covers`")
        if slug not in covers:
            raise TriageError(f"{doc}: `covers` does not include `{slug}`")
        for other in covers:
            if other != slug:
                require_entry(todo, store, other)
                settled = standing_settled(store, other)
                if settled is not None:
                    held.append((other, settled))
        held_slugs = {other for other, _ in held}
        covers = [s for s in covers if s not in held_slugs]
        for other in covers:
            other_data = store.load(other)
            if other != slug and review_state(other_data) == Review.RETRIAGE:
                reviews += [(other, path) for path in review_notes(other_data)]
        entry["covers"] = covers
    elif step == "contrarian":
        # The contrarian argues about the sketch's cluster; only the sketch opens passes.
        covers = sketch_covers(record, slug)
        entry["covers"] = covers
    landed: list[str] = []
    for target in covers:
        if target == slug:
            target_data, target_record = data, record
        else:
            target_data = store.load(target)
            target_record = open_pass(target_data)
            if target_data is None or target_record is None:
                target_data, target_record = start_pass(store, target)
        if rel not in registered(target_record):
            target_record["docs"].append(dict(entry))
            store.save(target, target_data)
            landed.append(target)
    return [Logged(rel, landed, held, reviews)]


def meta_line(document: Document, key: str) -> int:
    """File line of the first frontmatter line naming `key:`, else the frontmatter start."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    for offset, line in enumerate(document.meta_lines):
        if pattern.match(line):
            return document.meta_start + offset
    return document.meta_start or 1


def validate_ruling(fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Check one slug's ruling fields; returns the normalised ruling and its errors."""
    errors: list[str] = []
    disposition = fields.get("disposition")
    if disposition not in Disposition:
        errors.append(f"disposition {disposition!r} is not one of {', '.join(Disposition)}")
    priority = fields.get("priority")
    priority_ok = (not isinstance(priority, bool) and isinstance(priority, int)
                   and priority in PRIORITIES)
    if disposition == Disposition.DO and not priority_ok:
        errors.append(f"priority must be {PRIORITIES[0]}-{PRIORITIES[-1]} for `do`, "
                      f"got {priority!r}")
    elif disposition == Disposition.BLOCKED and priority is not None and not priority_ok:
        errors.append(f"priority must be {PRIORITIES[0]}-{PRIORITIES[-1]} when given for "
                      f"`blocked`, got {priority!r}")
    elif disposition not in (Disposition.DO, Disposition.BLOCKED) and priority is not None:
        errors.append(f"priority is only for `do` or `blocked`, got {priority!r} "
                      f"with {disposition!r}")
    blocked_on = fields.get("blocked_on")
    if isinstance(blocked_on, (str, int)):
        blocked_on = [blocked_on]
    if isinstance(blocked_on, list):
        blocked_on = [slug_text(b) for b in blocked_on]
    blocked_ok = isinstance(blocked_on, list) and bool(blocked_on) and all(
        isinstance(b, str) and b.strip() for b in blocked_on)
    if disposition == Disposition.BLOCKED and not blocked_ok:
        errors.append("blocked_on must be a non-empty list for `blocked`")
    elif disposition != Disposition.BLOCKED and blocked_on:
        errors.append(f"blocked_on is only for `blocked`, not {disposition!r}")
    defer_until = fields.get("defer_until")
    defer_ok = isinstance(defer_until, str) and bool(defer_until.strip())
    if disposition == Disposition.DEFER and not defer_ok:
        errors.append("defer_until must be non-empty for `defer`")
    elif disposition != Disposition.DEFER and defer_until:
        errors.append(f"defer_until is only for `defer`, not {disposition!r}")
    summary = fields.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary is missing")
    ruling = {
        "disposition": disposition,
        "priority": priority if disposition in (Disposition.DO, Disposition.BLOCKED) else None,
        "blocked_on": list(blocked_on) if disposition == Disposition.BLOCKED and blocked_ok else None,
        "defer_until": defer_until if disposition == Disposition.DEFER else None,
        "summary": summary,
    }
    return ruling, errors


def blocked_on_errors(todo: TodoFile, store: Store, slug: str,
                      ruling: dict[str, Any]) -> list[str]:
    """Blockers of a `blocked` ruling with a priority that the store cannot see clear.

    Each blocker must be another entry in the file, so that its leaving is observable
    while `slug` is still there. Cycles through other entries' rulings are not detected:
    like any blocked ruling whose blockers never leave, such an entry returns by age.
    A ruling without a priority is not checked.
    """
    if ruling.get("disposition") != Disposition.BLOCKED or ruling.get("priority") is None:
        return []
    errors = []
    for dep in ruling.get("blocked_on") or []:
        try:
            require_entry(todo, store, dep)
        except TriageError as exc:
            errors.append(f"priority on blocked needs every blocked_on to be an entry: {exc}")
            continue
        if dep == slug:
            errors.append("priority on blocked needs every blocked_on to be another entry; "
                          f'"{slug}" blocks on itself')
    return errors


def sketch_covers(record: dict[str, Any], slug: str) -> list[str]:
    sketches = [d for d in record.get("docs") or [] if d.get("step") == "sketch"]
    return list(sketches[-1].get("covers") or [slug]) if sketches else [slug]


def close_pass(store: Store, todo: TodoFile, slug: str, data: dict[str, Any],
               record: dict[str, Any], ruling: dict[str, Any]) -> None:
    in_file = todo.by_slug()
    if ruling.get("disposition") == Disposition.BLOCKED:
        # Which dependencies were entries at ruling time: one that later leaves the
        # file resolves the block even if it was never triaged.
        ruling["blocked_on_entries"] = [b for b in ruling.get("blocked_on") or []
                                        if b in in_file]
    record["ruling"] = ruling
    entry = in_file.get(slug)
    data["entry_hash"] = entry.body_hash if entry else None
    store.save(slug, data)


def record_ruling(store: Store, todo: TodoFile, slug: str,
                  path: Path) -> tuple[list[str], list[str]]:
    """Validate and store a ruling document; returns (slugs ruled, errors)."""
    data, record = require_open(store, slug)
    rel = in_store(store, path)
    try:
        document = read_document(path)
    except TriageError as exc:
        reject(store, slug, data, record, rel, str(exc))
        return [], [str(exc)]
    errors: list[str] = []
    rulings = document.meta.get("rulings")
    if not isinstance(rulings, dict) or not rulings:
        errors.append(f"{path}:{document.meta_start or 1}: frontmatter has no `rulings` mapping")
        rulings = {}
    rulings = {str(slug_text(key)): fields for key, fields in rulings.items()}
    for covered in sketch_covers(record, slug):
        if covered not in rulings:
            errors.append(f"{path}:{meta_line(document, 'rulings')}: `{covered}` is covered "
                          "by the sketch but has no ruling")
    normalised: dict[str, dict[str, Any]] = {}
    for other, fields in rulings.items():
        where = f"{path}:{meta_line(document, other)}"
        settled = standing_settled(store, other) if other != slug else None
        if settled is not None:
            errors.append(f"{where}: `{other}` is {settled.sentence()}; "
                          "a sketch cannot re-rule it — drop it "
                          f"from `rulings`, or the user runs `todo-triage override {other}`")
        elif other != slug and open_pass(store.load(other)) is None:
            errors.append(f"{where}: `{other}` has no open pass")
        if not isinstance(fields, dict):
            errors.append(f"{where}: `{other}`: ruling must be a mapping")
            continue
        ruling, problems = validate_ruling(fields)
        if not problems:
            problems = blocked_on_errors(todo, store, other, ruling)
        errors += [f"{where}: `{other}`: {problem}" for problem in problems]
        normalised[other] = ruling
    if errors:
        reject(store, slug, data, record, rel, "; ".join(e.split(": ", 1)[1] for e in errors))
        return [], errors
    cluster = list(normalised) if len(normalised) > 1 else []
    doc = {"step": "ruling", "path": rel, "date": today().isoformat()}
    for other, ruling in normalised.items():
        other_data, other_record = (data, record) if other == slug else require_open(store, other)
        if rel not in registered(other_record):
            other_record["docs"].append(dict(doc))
        close_pass(store, todo, other, other_data, other_record, {
            **ruling, "cluster": cluster, "ruled_by": RuledBy.AGENT.value,
            "date": today().isoformat()})
    return list(normalised), []


def reject(store: Store, slug: str, data: dict[str, Any], record: dict[str, Any], rel: str,
           reason: str) -> None:
    record["docs"].append({"step": "ruling", "path": rel, "date": today().isoformat(),
                           "rejected": reason})
    store.save(slug, data)


def abandon(store: Store, slugs: list[str]) -> list[tuple[str, int]]:
    """End each slug's open pass without a ruling; all or nothing. Returns (slug, T)."""
    targets = []
    missing = []
    for slug in dict.fromkeys(slugs):
        data = store.load(slug)
        record = open_pass(data)
        if data is None or record is None:
            missing.append(slug)
        else:
            targets.append((slug, data, record))
    if missing:
        raise TriageError("no open pass to abandon: " + ", ".join(f"`{s}`" for s in missing)
                          + "; nothing written")
    stamp = now_stamp()
    for slug, data, record in targets:
        record["abandoned"] = stamp
        store.save(slug, data)
    return [(slug, int(record["pass"])) for slug, _, record in targets]


def override(store: Store, todo: TodoFile, slug: str, fields: dict[str, Any]) -> dict[str, Any]:
    if slug not in todo.by_slug() and store.load(slug) is None:
        raise TriageError(f"`{slug}` is neither in {store.config.todo_path} nor in the store")
    if slug in store.config.ignore:
        raise TriageError(f"`{slug}` is ignored in store.yaml")
    ruling, errors = validate_ruling({**fields, "summary": fields.get("summary") or "-"})
    errors = errors or blocked_on_errors(todo, store, slug, ruling)
    if errors:
        raise TriageError("; ".join(errors))
    ruling["summary"] = fields.get("summary") or None
    data = store.load(slug)
    record = open_pass(data)
    if data is None or record is None:
        data, record = start_pass(store, slug)
    ruling = {**ruling, "cluster": [], "ruled_by": RuledBy.USER.value, "date": today().isoformat()}
    close_pass(store, todo, slug, data, record, ruling)
    return ruling


# `modified` and `open` rulings no longer answer for the entry's text; a `stale`
# ruling is due for re-triage but still stands.
READY_STATUSES = (Status.TRIAGED, Status.STALE)


def eligible(row: Row) -> bool:
    """In the file, its ruling standing and settled, with no open attempt."""
    return (row.entry is not None and row.status in READY_STATUSES and row.settled
            and not open_attempts(row.data))


@dataclass(frozen=True)
class Scope:
    """The session's filters: exactly `priority`, `max_priority` or better, only `slugs`
    when given, never `exclude`."""

    priority: int | None = None
    max_priority: int | None = None
    slugs: frozenset[str] | None = None
    exclude: frozenset[str] = frozenset()

    def admits(self, row: Row) -> bool:
        """Whether `row` passes the filters."""
        if row.slug in self.exclude:
            return False
        if self.slugs is not None and row.slug not in self.slugs:
            return False
        if self.priority is not None and row.priority != self.priority:
            return False
        if self.max_priority is not None and (row.priority is None
                                              or row.priority > self.max_priority):
            return False
        return True


def priority_rank(row: Row) -> int:
    """Sort key putting higher priorities first and a missing priority last."""
    return row.priority if row.priority is not None else PRIORITIES[-1] + 1


def blockers_within(row: Row, slugs: set[str]) -> bool:
    """Whether every blocker of `row` has cleared or is one of `slugs`."""
    return all(state == BlockerState.CLEARED or dep in slugs for dep, state in row.blockers)


def ready(all_rows: list[Row], scope: Scope) -> list[Row]:
    """Settled `do` entries, and settled `blocked` entries with a priority whose blockers
    have all left TODO.md, whose ruling stands, with no open attempt, in scope; unranked."""
    return [row for row in all_rows
            if eligible(row) and row.effective_disposition == Disposition.DO
            and scope.admits(row)]


@dataclass(frozen=True)
class Follower:
    """An entry a ready entry unlocks, with the entries it unlocks in turn."""

    row: Row
    unlocks: tuple[Follower, ...]


# How many levels of followers `followers` offers beneath a ready entry.
CHAIN_DEPTH = 2


def blocked_with_priority(row: Row) -> bool:
    """Whether `row` is ruled `blocked` with a priority."""
    return row.disposition == Disposition.BLOCKED and row.priority is not None


def followers(all_rows: list[Row], ready_rows: list[Row],
              scope: Scope) -> dict[str, tuple[Follower, ...]]:
    """Per ready slug, the eligible, in-scope `blocked` entries with a priority that
    become ready once it ships, `CHAIN_DEPTH` levels deep.

    A candidate sits at the shallowest level where it blocks on a slug of the level
    above (its parents) and every other blocker has cleared or lies on a path from a
    ready entry to one of its parents; it is listed under each parent. Every open
    blocker of an offered follower is thus on a path it is shown on. A candidate with a
    deleted blocker, or an open one on none of its parents' paths, is not offered. Each
    list is ordered by priority, then file order.
    """
    candidates = [row for row in all_rows
                  if eligible(row) and blocked_with_priority(row) and not row.unblocked
                  and scope.admits(row)]
    # Per placed slug, the slugs on the paths from a ready entry down to it, inclusive.
    paths = {row.slug: {row.slug} for row in ready_rows}
    levels: list[list[Row]] = [list(ready_rows)]
    for _ in range(CHAIN_DEPTH):
        above = {row.slug for row in levels[-1]}
        level: list[Row] = []
        for row in candidates:
            parents = [dep for dep, _ in row.blockers if dep in above]
            if row.slug in paths or not parents:
                continue
            reach = set[str]().union(*(paths[dep] for dep in parents))
            if blockers_within(row, reach):
                level.append(row)
                paths[row.slug] = reach | {row.slug}
        levels.append(level)

    def beneath(depth: int, slug: str) -> tuple[Follower, ...]:
        if depth >= len(levels):
            return ()
        under = sorted((row for row in levels[depth]
                        if any(dep == slug for dep, _ in row.blockers)), key=priority_rank)
        return tuple(Follower(row, beneath(depth + 1, row.slug)) for row in under)

    return {row.slug: beneath(1, row.slug) for row in ready_rows}


def start_attempt(store: Store, todo: TodoFile, selection: Path, adr_root: Path) -> Path:
    document = read_document(selection)
    slugs = slug_list(document.meta.get("slugs"), f"{selection}: `slugs`")
    by_slug = {row.slug: row for row in rows(store, todo)}
    selected = set(slugs)
    for slug in slugs:
        require_entry(todo, store, slug)
        if open_attempts(store.load(slug)):
            raise TriageError(f"`{slug}` already has an open attempt")
        row = by_slug[slug]
        if eligible(row) and blocked_with_priority(row):
            outside = [f"`{dep}` ({state})" for dep, state in row.blockers
                       if state != BlockerState.CLEARED and dep not in selected]
            if outside:
                raise TriageError(f"`{slug}` is not ready: blocked on {', '.join(outside)}, "
                                  "neither resolved nor in this selection")
        if not (eligible(row) and (row.effective_disposition == Disposition.DO
                                   or blocked_with_priority(row))):
            raise TriageError(f"`{slug}` is not ready: {row.status}, ruled "
                              f"{row.disposition or 'nothing'}, review {row.review}; only "
                              "standing, settled `do` rulings, or `blocked` rulings with a "
                              "priority whose blockers are all resolved or in this selection, "
                              "can be attempted")
    now = dt.datetime.now()
    parent = adr_root / now.strftime("%Y") / now.strftime("%m")
    parent.mkdir(parents=True, exist_ok=True)
    adr = unused(parent, f"{now.strftime('%d')}-{store.config.tag}--todo-{slugs[0]}")
    adr.mkdir()
    body = document.body.lstrip("\n")
    (adr / "user-request.md").write_text(DISCLAIMER + "\n\n" + body, encoding="utf-8")
    attempt = {"adr_dir": str(adr.resolve()), "selection": str(selection.resolve()),
               "slugs": slugs, "started": now_stamp(), "closed": None, "outcome": None}
    for slug in slugs:
        data = store.load(slug) or {"slug": slug}
        data.setdefault("attempts", []).append(dict(attempt))
        store.save(slug, data)
    return adr


def close_attempt(store: Store, todo: TodoFile, adr: Path,
                  outcome: str | None) -> tuple[str, list[str]]:
    target = str(adr.resolve())
    matches: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for slug in store.slugs():
        data = store.load(slug) or {}
        for attempt in data.get("attempts") or []:
            if attempt.get("adr_dir") == target:
                matches.append((slug, data, attempt))
    if not matches:
        raise TriageError(f"no attempt recorded for {target}")
    still_open = [(s, d, a) for s, d, a in matches if not a.get("closed")]
    if not still_open:
        raise TriageError(f"the attempt for {target} is already closed")
    covered = [s for s, _, _ in still_open]
    if outcome is None:
        in_file = todo.by_slug()
        outcome = (Outcome.SHIPPED if all(s not in in_file for s in covered)
                   else Outcome.INCOMPLETE).value
    stamp = now_stamp()
    for slug, data, attempt in still_open:
        attempt["closed"] = stamp
        attempt["outcome"] = outcome
        store.save(slug, data)
    return outcome, covered


def prunable(all_rows: list[Row]) -> list[Row]:
    """Entries whose standing, settled ruling is `delete`; a `modified` or `open` one is re-triaged first."""
    return [r for r in all_rows
            if r.entry is not None and r.status in READY_STATUSES
            and r.settled_delete]


def leftover_sites(all_rows: list[Row], sites: list[Site]) -> list[Site]:
    """Sites of slugs gone from TODO.md, not ignored, whose standing, settled ruling is `delete`."""
    gone = {r.slug for r in all_rows
            if r.entry is None and r.status != Status.IGNORED
            and r.settled_delete}
    return [s for s in sites if s.slug in gone]


def prune_text(text: str, headings: set[int]) -> str:
    """Remove each block whose `## ` heading is at a 1-based line in `headings`."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if ENTRY_START_RE.match(line)]
    drop: set[int] = set()
    for index, start in enumerate(starts):
        if start + 1 in headings:
            stop = starts[index + 1] if index + 1 < len(starts) else len(lines)
            drop.update(range(start, stop))
    kept = [line for i, line in enumerate(lines) if i not in drop]
    return "\n".join(kept).rstrip("\n") + "\n"


def mark_pruned(store: Store, slugs: list[str]) -> None:
    for slug in slugs:
        data = store.load(slug) or {"slug": slug}
        data["pruned"] = today().isoformat()
        store.save(slug, data)


def ruling_rows(all_rows: list[Row]) -> list[tuple[str, dict[str, Any]]]:
    """Every (slug, ruling) in `all_rows`, all passes, in row order."""
    out = []
    for row in all_rows:
        for record in (row.data or {}).get("passes") or []:
            if record.get("ruling") is not None:
                out.append((row.slug, record["ruling"]))
    return out



@dataclass(frozen=True)
class Reviewed:
    """One slug `review` acted on: `changed` False when its mate was ruled separately since,
    has a pass open (`open_ordinal`), or has no ruling in the store (`ruling` None)."""

    slug: str
    changed: bool
    ruling: dict[str, Any] | None
    next_pass: int
    open_ordinal: int | None = None


def review(store: Store, todo: TodoFile, slug: str, state: Review,
           note: str | None) -> tuple[Path | None, list[Reviewed]]:
    """Set the user's review on `slug`'s latest ruled pass and on each cluster mate's.

    A mate is changed only while its `mate_standing` is `LIVE`. With a note, one notes file is written in `slug`'s entry dir and
    registered in every pass the review is set on.
    """
    if state == Review.RETRIAGE:
        require_entry(todo, store, slug)
        if not (note or "").strip():
            raise TriageError("a re-triage needs a note: --note TEXT or --notes-file PATH|-")
    data = store.load(slug)
    current = open_pass(data)
    if current is not None:
        raise TriageError(f"`{slug}`: pass {current['pass']} is open; wait for its ruling, "
                          f"or `todo-triage abandon {slug}` it")
    record = latest_ruled_pass(data)
    if data is None or record is None:
        raise TriageError(f"`{slug}` has no ruling to review")
    ruling = record["ruling"]
    if user_ruled(ruling):
        raise TriageError(f"`{slug}` is ruled by the user ({ruling.get('date')}); "
                          f"`todo-triage override {slug}` changes it")
    date = today().isoformat()
    path: Path | None = None
    doc: dict[str, Any] | None = None
    if note is not None and note.strip():
        t = int(record["pass"])
        directory = store.entry_dir(slug)
        directory.mkdir(parents=True, exist_ok=True)
        path = unused(directory, f"review-user-t{t}", ".md")
        header = f"# Review of {slug} pass {t}, {date}: {state}"
        path.write_text(f"{header}\n\n{note.rstrip()}\n", encoding="utf-8")
        doc = {"step": "review", "path": store.relative(path), "date": date}
    mapping = {"state": state.value, "date": date}

    def mark(target: str, target_data: dict[str, Any], target_record: dict[str, Any]) -> None:
        target_record["review"] = dict(mapping)
        if doc is not None and doc["path"] not in registered(target_record):
            target_record.setdefault("docs", []).append(dict(doc))
        store.save(target, target_data)

    mark(slug, data, record)
    out = [Reviewed(slug, True, ruling, next_pass_ordinal(data))]
    for mate in ruling.get("cluster") or []:
        if mate == slug:
            continue
        mate_data = store.load(mate)
        standing = mate_standing(slug, mate_data)
        mate_record = latest_ruled_pass(mate_data)
        mate_open = open_pass(mate_data)
        if mate_data is None or mate_record is None:
            out.append(Reviewed(mate, False, None, next_pass_ordinal(mate_data)))
            continue
        mate_ruling = mate_record["ruling"]
        if standing != MateStanding.LIVE:
            out.append(Reviewed(mate, False, mate_ruling, next_pass_ordinal(mate_data),
                                int(mate_open["pass"]) if mate_open is not None else None))
            continue
        mark(mate, mate_data, mate_record)
        out.append(Reviewed(mate, True, mate_ruling, next_pass_ordinal(mate_data)))
    return path, out


def ruled_pass_docs(row: Row, steps: tuple[str, ...] = ("ruling", "sketch", "contrarian")
                    ) -> dict[str, str | None]:
    """Store-relative path of the last unrejected document of each step in `steps` on the
    latest ruled pass; None for a step with none."""
    record = latest_ruled_pass(row.data) or {}
    return {step: last_unrejected(record, step) for step in steps}


def unrejected(record: dict[str, Any], step: str) -> list[str]:
    """Store-relative paths of `record`'s unrejected documents of `step`, in registration order."""
    return [d["path"] for d in record.get("docs") or []
            if d.get("step") == step and not d.get("rejected")]


def last_unrejected(record: dict[str, Any], step: str) -> str | None:
    """Store-relative path of `record`'s last unrejected document of `step`, or None."""
    paths = unrejected(record, step)
    return paths[-1] if paths else None


def ruled_pass_before(data: dict[str, Any] | None, t: int) -> dict[str, Any] | None:
    """The latest pass numbered below `t` that carries a ruling, or None; abandoned passes
    carry none and are skipped."""
    earlier = [p for p in (data or {}).get("passes") or [] if int(p.get("pass", 0)) < t]
    return next((p for p in reversed(earlier) if p.get("ruling") is not None), None)


@dataclass(frozen=True)
class PriorPass:
    """A slug's ruled pass before its latest one; paths store-relative."""

    slug: str
    ordinal: int
    ruling: str | None  # None when the pass has no unrejected ruling document
    ruled_by: str | None
    review: str | None
    state: str | None


def cluster_pass_history(row: Row, mates: list[Row]) -> tuple[list[str], list[PriorPass]]:
    """The explorations and previous ruled passes behind `row`'s ruling, joined across
    `row` and its live `mates` in that order; paths store-relative.

    Explorations: the unrejected `exploration` documents of each slug's latest ruled pass,
    each path once. Previous: per slug with a ruled pass before its latest one, that pass's
    ordinal, last unrejected ruling and review documents, `ruled_by` and review state.
    """
    explorations: list[str] = []
    previous: list[PriorPass] = []
    for member in [row, *mates]:
        latest = latest_ruled_pass(member.data)
        if latest is None:
            continue
        for path in unrejected(latest, "exploration"):
            if path not in explorations:
                explorations.append(path)
        prior = ruled_pass_before(member.data, int(latest.get("pass", 0)))
        if prior is None:
            continue
        previous.append(PriorPass(
            slug=member.slug,
            ordinal=int(prior["pass"]),
            ruling=last_unrejected(prior, "ruling"),
            ruled_by=prior["ruling"].get("ruled_by"),
            # TODO(correctness-previous-review-drops-all-but-last-note): a pass reviewed
            # more than once with a note carries several; only the last is reported here.
            review=last_unrejected(prior, "review"),
            state=(prior.get("review") or {}).get("state"),
        ))
    return explorations, previous

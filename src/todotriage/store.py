"""The triage store: `store.yaml`, one `entries/<slug>/triage.yaml` per slug, and status."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from . import TriageError
from .todomd import Entry, TodoFile

HEADER = """\
# TODO triage store. Written and read by the `todo-triage` tool as part of the
# triage and burndown workflows. LLM agents: never create, edit, copy or
# imitate this file or any triage.yaml outside those workflows; the tool is
# the only writer. Rulings in here were made by the product owner or a
# product-owner proxy and are not to be reproduced in TODO.md.
"""

SCHEMA = 1
DEFAULT_TODO_PATH = "TODO.md"
PRIORITIES = range(1, 5)


class Disposition(StrEnum):
    """A ruling's verdict on a TODO entry."""

    DO = "do"
    DEFER = "defer"
    BLOCKED = "blocked"
    DELETE = "delete"


class Status(StrEnum):
    """Derived per entry by `rows()`; never stored."""

    UNTRIAGED = "untriaged"
    TRIAGED = "triaged"
    MODIFIED = "modified"
    STALE = "stale"
    OPEN = "open"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class Review(StrEnum):
    """The user's review of an entry's latest ruled pass."""

    NONE = "-"
    PENDING = "pending"
    APPROVED = "approved"
    RETRIAGE = "retriage"


REVIEW_STATES = (Review.APPROVED, Review.RETRIAGE)


class RuledBy(StrEnum):
    """Who made a ruling; stored as its plain `.value`."""

    AGENT = "agent"
    USER = "user"


class Outcome(StrEnum):
    """Stored as its plain `.value`: the YAML safe dumper rejects enum members."""

    SHIPPED = "shipped"
    INCOMPLETE = "incomplete"
    STOPPED = "stopped"


def today() -> dt.date:
    return dt.date.today()


def now_stamp() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        return dt.date.fromisoformat(value[:10])
    return None


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write header + YAML atomically, so a crash never leaves a half-written file."""
    text = HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TriageError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise TriageError(f"{path}: expected a mapping at top level")
    return data


@dataclass(frozen=True)
class Config:
    tag: str
    todo_path: str
    stale_days: int
    ignore: tuple[str, ...]


class Store:
    def __init__(self, root: Path, config: Config) -> None:
        self.root = root
        self.config = config

    @classmethod
    def open(cls, root: Path) -> Store:
        path = root / "store.yaml"
        if not path.is_file():
            raise TriageError(f"{path} does not exist; run `todo-triage init` first")
        data = load_yaml(path)
        if data.get("schema") != SCHEMA:
            raise TriageError(f"{path}: unsupported schema {data.get('schema')!r}")
        try:
            config = Config(
                tag=str(data["tag"]),
                todo_path=str(data.get("todo_path", DEFAULT_TODO_PATH)),
                stale_days=int(data.get("stale_days", 180)),
                ignore=tuple(str(s) for s in data.get("ignore") or ()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TriageError(f"{path}: malformed config: {exc}") from None
        return cls(root, config)

    @classmethod
    def init(cls, root: Path, tag: str, todo_path: str, stale_days: int,
             ignore: list[str]) -> Store:
        path = root / "store.yaml"
        if path.exists():
            raise TriageError(f"{path} already exists")
        if not tag.strip():
            raise TriageError("--tag must be non-empty")
        if stale_days < 1:
            raise TriageError("--stale-days must be at least 1")
        dump_yaml(path, {
            "schema": SCHEMA,
            "tag": tag,
            "todo_path": todo_path,
            "stale_days": stale_days,
            "ignore": list(ignore),
        })
        (root / "entries").mkdir(exist_ok=True)
        (root / "sessions").mkdir(exist_ok=True)
        return cls.open(root)

    def entry_dir(self, slug: str) -> Path:
        return self.root / "entries" / slug

    def triage_path(self, slug: str) -> Path:
        return self.entry_dir(slug) / "triage.yaml"

    def load(self, slug: str) -> dict[str, Any] | None:
        path = self.triage_path(slug)
        return load_yaml(path) if path.is_file() else None

    def save(self, slug: str, data: dict[str, Any]) -> None:
        dump_yaml(self.triage_path(slug), data)

    def slugs(self) -> list[str]:
        entries = self.root / "entries"
        if not entries.is_dir():
            return []
        return sorted(p.parent.name for p in entries.glob("*/triage.yaml"))

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()


def passes(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((data or {}).get("passes") or [])


def ended(record: dict[str, Any]) -> bool:
    """A pass ends by a `ruling` or by an `abandoned` stamp, never both."""
    return record.get("ruling") is not None or bool(record.get("abandoned"))


def open_pass(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """The latest pass, when it has neither a ruling nor an `abandoned` stamp."""
    records = passes(data)
    return records[-1] if records and not ended(records[-1]) else None


def latest_ruled_pass(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """The last pass carrying a ruling, whatever open or abandoned pass came after it."""
    for record in reversed(passes(data)):
        if record.get("ruling") is not None:
            return record
    return None


def latest_ruling(data: dict[str, Any] | None) -> dict[str, Any] | None:
    record = latest_ruled_pass(data)
    return record["ruling"] if record else None


def user_ruled(ruling: dict[str, Any] | None) -> bool:
    """Whether `ruling` exists and was made by the user."""
    return ruling is not None and ruling.get("ruled_by") == RuledBy.USER


def review_state(data: dict[str, Any] | None) -> Review:
    """A user ruling is approved unless the user sent it back; an agent ruling by its pass's `review`."""
    record = latest_ruled_pass(data)
    if record is None:
        return Review.NONE
    state = (record.get("review") or {}).get("state")
    if user_ruled(record["ruling"]):
        return Review.RETRIAGE if state == Review.RETRIAGE else Review.APPROVED
    return Review(state) if state in REVIEW_STATES else Review.PENDING


def is_settled(data: dict[str, Any] | None) -> bool:
    """Whether the standing ruling is the user's and not sent back, or user-approved."""
    return review_state(data) == Review.APPROVED


def is_settled_delete(data: dict[str, Any] | None) -> bool:
    """Whether the standing ruling is `delete` and settled."""
    ruling = latest_ruling(data)
    return ruling is not None and ruling.get("disposition") == Disposition.DELETE and is_settled(data)


class BlockerState(StrEnum):
    """One `blocked_on` item as `rows()` sees it."""

    OPEN = "open"
    CLEARED = "cleared"  # left the file without a settled `delete` ruling
    DELETED = "deleted"  # ruled `delete` and settled, whether or not pruned yet


# How a stale reason names a blocker that has left; covers every non-open BlockerState.
BLOCKER_STALE_REASONS = {BlockerState.CLEARED: "resolved", BlockerState.DELETED: "ruled delete"}


def unblocked_reason(ruling: dict[str, Any]) -> str:
    """Why a `blocked` ruling with a priority is ready: its blockers and its priority."""
    names = ", ".join(f"`{dep}`" for dep in ruling.get("blocked_on") or [])
    return f"blocked on {names}, resolved; ready at P{ruling.get('priority')}"


class MateStanding(StrEnum):
    """Whether a slug named in a ruling's cluster still shares that ruling."""

    LIVE = "live"
    NO_RULING = "no ruling"
    OPEN = "open"
    SEPARATE = "ruled separately"


def mate_standing(slug: str, mate_data: dict[str, Any] | None) -> MateStanding:
    """`LIVE` when the mate has no open pass and its latest ruling names `slug` in its cluster."""
    ruling = latest_ruling(mate_data)
    if ruling is None:
        return MateStanding.NO_RULING
    if open_pass(mate_data) is not None:
        return MateStanding.OPEN
    if slug not in (ruling.get("cluster") or []):
        return MateStanding.SEPARATE
    return MateStanding.LIVE


def review_notes(data: dict[str, Any] | None) -> list[str]:
    """Store-relative paths of the review-notes documents on the latest ruled pass."""
    record = latest_ruled_pass(data) or {}
    return [d["path"] for d in record.get("docs") or []
            if d.get("step") == "review" and not d.get("rejected")]


def next_pass_ordinal(data: dict[str, Any] | None) -> int:
    return max((int(p.get("pass", 0)) for p in passes(data)), default=0) + 1


def open_attempts(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [a for a in (data or {}).get("attempts") or [] if not a.get("closed")]


@dataclass(frozen=True)
class Row:
    """One slug's derived state: the join of TODO.md and the store."""

    slug: str
    status: Status
    entry: Entry | None
    data: dict[str, Any] | None
    reason: str = ""
    # A `blocked` ruling with a priority whose blockers have all left the file.
    unblocked: bool = False
    # A `triaged` or `stale` `blocked` ruling's `blocked_on` items with their states,
    # in ruling order; empty for every other row.
    blockers: tuple[tuple[str, BlockerState], ...] = ()

    @property
    def ruling(self) -> dict[str, Any] | None:
        return latest_ruling(self.data)

    @property
    def review(self) -> Review:
        return review_state(self.data)

    @property
    def settled(self) -> bool:
        """Whether the standing ruling is the user's and not sent back, or user-approved."""
        return is_settled(self.data)

    @property
    def settled_delete(self) -> bool:
        """Whether the standing ruling is `delete` and settled."""
        return is_settled_delete(self.data)

    @property
    def disposition(self) -> str | None:
        ruling = self.ruling
        return ruling.get("disposition") if ruling else None

    @property
    def effective_disposition(self) -> str | None:
        """`do` for an unblocked row, else the disposition as ruled."""
        return Disposition.DO if self.unblocked else self.disposition

    @property
    def priority(self) -> int | None:
        ruling = self.ruling
        return ruling.get("priority") if ruling else None

    @property
    def ruled_date(self) -> dt.date | None:
        ruling = self.ruling
        return as_date(ruling.get("date")) if ruling else None

    @property
    def cluster(self) -> list[str]:
        ruling = self.ruling
        return list(ruling.get("cluster") or []) if ruling else []

    @property
    def blocked_on(self) -> list[str]:
        """The ruling's blockers as strings; empty without a ruling."""
        ruling = self.ruling
        return [str(b) for b in ruling.get("blocked_on") or []] if ruling else []


def rows(store: Store, todo: TodoFile, on: dt.date | None = None) -> list[Row]:
    """Every listed slug (TODO file order, then fragments) then every store-only slug (sorted)."""
    on = on or today()
    ignore = set(store.config.ignore)
    in_file = todo.by_slug()
    order = list(in_file) + [s for s in store.slugs() if s not in in_file]

    base: dict[str, tuple[Status, str]] = {}
    loaded: dict[str, dict[str, Any] | None] = {}
    for slug in order:
        data = store.load(slug)
        loaded[slug] = data
        entry = in_file.get(slug)
        ruling = latest_ruling(data)
        if slug in ignore:
            base[slug] = (Status.IGNORED, "")
        elif entry is None:
            base[slug] = (Status.RESOLVED, "")
        elif open_pass(data) is not None:
            base[slug] = (Status.OPEN, "")
        elif ruling is None:
            base[slug] = (Status.UNTRIAGED, "")
        elif (data or {}).get("entry_hash") != entry.body_hash:
            base[slug] = (Status.MODIFIED, "body changed since the ruling")
        else:
            ruled = as_date(ruling.get("date"))
            if ruled is None or (on - ruled).days > store.config.stale_days:
                base[slug] = (Status.STALE, f"ruled {ruled}, older than {store.config.stale_days} days")
            else:
                base[slug] = (Status.TRIAGED, "")

    def blocker_state(dep: str, were_entries: set[str]) -> BlockerState:
        if is_settled_delete(loaded.get(dep)):
            return BlockerState.DELETED
        dep_status = base.get(dep, (None, ""))[0]
        if dep_status == Status.RESOLVED or (dep_status is None and dep in were_entries):
            return BlockerState.CLEARED
        return BlockerState.OPEN

    out: list[Row] = []
    for slug in order:
        status, reason = base[slug]
        data = loaded[slug]
        ruling = latest_ruling(data)
        unblocked = False
        blockers: tuple[tuple[str, BlockerState], ...] = ()
        if (status in (Status.TRIAGED, Status.STALE) and ruling
                and ruling.get("disposition") == Disposition.BLOCKED):
            blocked_on = list(ruling.get("blocked_on") or [])
            were_entries = set(ruling.get("blocked_on_entries") or ())
            states = [(dep, blocker_state(dep, were_entries)) for dep in blocked_on]
            blockers = tuple(states)
            with_priority = ruling.get("priority") is not None
            # Without a priority any blocker leaving returns the entry; with one, only a
            # deleted blocker does, and every blocker cleared makes it ready instead.
            returns = ({BlockerState.DELETED} if with_priority
                       else {BlockerState.DELETED, BlockerState.CLEARED})
            first = next(((dep, state) for dep, state in states if state in returns), None)
            if status == Status.TRIAGED and first is not None:
                dep, state = first
                status = Status.STALE
                reason = f"blocked on `{dep}`, which is {BLOCKER_STALE_REASONS[state]}"
            elif with_priority and states and all(s == BlockerState.CLEARED for _, s in states):
                unblocked = True
                if status == Status.TRIAGED:
                    reason = unblocked_reason(ruling)
        out.append(Row(slug=slug, status=status, entry=in_file.get(slug), data=data,
                       reason=reason, unblocked=unblocked, blockers=blockers))
    return out

"""The TODO.md grammar.

A `# ` title, then `## `-headed entries, each to the next `## `. Headings are
found outside fenced blocks only; a `## Open` section, a heading of level 3 or
deeper and a `# ` heading after the first entry are errors.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import TriageError

SLUG_PATTERN = r"`?(?P<slug>[a-z0-9][a-z0-9_-]*)`?(?P<trail>.*)$"
HEADING_RE = re.compile(r"^## " + SLUG_PATTERN)
TITLE_RE = re.compile(r"^# \S")
ATX_HEADING_RE = re.compile(r"^(#{1,6})( |$)")
FENCE_PREFIXES = ("```", "~~~")
SITE_TOKEN_RE = re.compile(r"TODO\(([^()\s]+)\)")
OPEN_SECTION = "Open"
# The directories `allthechanges` keeps one-entry-per-file fragments in.
FRAGMENT_DIRS = ("todo.d", ".todo.d")
FRAGMENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Text that records status, dates or rulings inside an entry; the store holds
# those, and an entry carrying them is a pattern other agents copy.
IN_BAND_MARKERS = (
    "[BLOCKED",
    "[blocked-on",
    "as-of",
    "NOT NOW",
    "TOMBSTONE",
    "Triaged",
    "triaged 20",
)

@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warning"
    path: str  # repo-relative file the issue is in
    line: int  # 1-based; 0 when the issue is not tied to a line
    message: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.level}: {where}: {self.message}"


@dataclass(frozen=True)
class Entry:
    slug: str
    path: str  # repo-relative: the TODO file, or the fragment file
    line: int  # 1-based line of the heading; 1 for a fragment
    end_line: int  # 1-based last line of the body (== line when a file entry's body is empty)
    stop: int  # TODO file: 1-based line of the next boundary, so the block is
    # `line`..`stop - 1`, trailing blanks included; 0 for a fragment
    body: str
    heading: str | None  # the full heading line, without its newline; None for a fragment
    backticked: bool  # True for a fragment
    trail: str  # "" for a fragment

    @property
    def fragment(self) -> bool:
        return self.heading is None

    @property
    def where(self) -> str:
        return self.path if self.fragment else f"{self.path}:{self.line}"

    @property
    def text(self) -> str:
        return self.body if self.heading is None else self.heading + "\n" + self.body

    @property
    def body_hash(self) -> str:
        return body_hash(self.body)


@dataclass
class TodoFile:
    title: str | None
    fragment_dir: str | None = None
    entries: list[Entry] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    sectioned: int | None = None  # 1-based line of a `## Open` heading, or None

    def by_slug(self) -> dict[str, Entry]:
        """First entry per slug; duplicates are a lint error, so later ones lose."""
        out: dict[str, Entry] = {}
        for entry in self.entries:
            out.setdefault(entry.slug, entry)
        return out

    def tracker(self, todo_path: str) -> str:
        """The tracker as messages name it: the TODO file, and the fragment directory if any."""
        return todo_path + (f" or {self.fragment_dir}" if self.fragment_dir else "")

    def refuse_errors(self, todo_path: str, action: str) -> None:
        """Raise TriageError when the tracker has an error-level issue."""
        if any(issue.level == "error" for issue in self.issues):
            raise TriageError(f"{self.tracker(todo_path)} has lint errors; fix them before {action}")

    def refuse_sectioned(self, todo_path: str) -> None:
        """Raise TriageError when the file is in the sectioned layout, which parses to no entries."""
        if self.sectioned is not None:
            raise TriageError(f"{todo_path}:{self.sectioned}: `## {OPEN_SECTION}` section: the "
                              "sectioned layout is not read; move its entries directly under "
                              "the title as ## `<slug>` (see `lint`)")


def body_hash(body: str) -> str:
    """sha256 of the body with whitespace runs collapsed and the ends stripped."""
    normalised = re.sub(r"\s+", " ", body).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def headings(lines: list[str]) -> tuple[list[tuple[int, int]], int | None]:
    """(0-based line, `#` count) of every ATX heading outside a fenced block, and the
    0-based line of the fence still open at the end, or None.

    Every line starting with three backticks or three tildes toggles the fenced
    state, whichever character opened the fence: the rule `allthechanges` reads
    and gathers by, so a file both tools read splits into the same blocks.
    """
    out: list[tuple[int, int]] = []
    opened: int | None = None
    for index, line in enumerate(lines):
        if line.startswith(FENCE_PREFIXES):
            opened = index if opened is None else None
        elif opened is None and (match := ATX_HEADING_RE.match(line)):
            out.append((index, len(match[1])))
    return out, opened


def heading_text(line: str, level: int) -> str:
    return line[level + 1:].rstrip()


def parse(text: str, path: str) -> TodoFile:
    """Split into entries and collect structural issues; never raises on content."""
    lines = text.split("\n")
    todo = TodoFile(title=None)

    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None and TITLE_RE.match(lines[first]):
        todo.title = lines[first][2:].strip()
    else:
        line = first + 1 if first is not None else 0
        todo.issues.append(Issue("error", path, line, "file does not start with a `# ` title"))

    found, open_fence = headings(lines)
    if open_fence is not None:
        todo.issues.append(Issue("error", path, open_fence + 1,
                                 f"fence line {lines[open_fence]!r} is never closed; "
                                 "no heading after it is read"))
    level_two = [i for i, level in found if level == 2]
    starts = [i for i in level_two if heading_text(lines[i], 2) != OPEN_SECTION]
    for index, level in found:
        if level == 2 and heading_text(lines[index], 2) == OPEN_SECTION:
            message = (f"`## {OPEN_SECTION}` section: the sectioned layout is not read; "
                       "entries go directly under the title as ## `<slug>`")
            if todo.sectioned is None:
                todo.sectioned = index + 1
        elif level >= 3:
            message = "heading below the entry level; entries have no sub-headings"
        elif level == 1 and starts and index > starts[0]:
            message = "`# ` heading after the first entry"
        else:
            continue
        todo.issues.append(Issue("error", path, index + 1, f"{message}: {lines[index]!r}"))
    bounds = [(start, next((i for i in level_two if i > start), len(lines))) for start in starts]
    add_entries(todo, path, lines, bounds)
    todo.issues.sort(key=lambda i: i.line)
    return todo


def add_entries(todo: TodoFile, path: str, lines: list[str],
                bounds: list[tuple[int, int]]) -> None:
    """Append one entry per 0-based `(heading, stop)` block, with its issues."""
    seen: dict[str, int] = {}
    for start, stop in bounds:
        heading = lines[start]
        body_lines = lines[start + 1:stop]
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        body = "\n".join(body_lines)
        line_no = start + 1
        match = HEADING_RE.match(heading)
        if match is None:
            todo.issues.append(Issue("error", path, line_no, f"unparseable heading: {heading!r}"))
            continue
        slug = match["slug"]
        rest = heading[heading.index(" ") + 1:]
        backticked = rest.startswith("`") and rest[1 + len(slug):].startswith("`")
        trail = match["trail"]
        todo.entries.append(Entry(
            slug=slug,
            path=path,
            line=line_no,
            end_line=line_no + len(body_lines),
            stop=stop + 1,
            body=body,
            heading=heading,
            backticked=backticked,
            trail=trail,
        ))
        if slug in seen:
            todo.issues.append(Issue("error", path, line_no,
                                     f"duplicate slug `{slug}` (first at line {seen[slug]})"))
        else:
            seen[slug] = line_no
        if not body.strip():
            todo.issues.append(Issue("error", path, line_no, f"`{slug}` has an empty body"))
        if not backticked:
            todo.issues.append(Issue("warning", path, line_no,
                                     f"`{slug}`: slug is not backticked"))
        if trail.strip():
            todo.issues.append(Issue("warning", path, line_no,
                                     f"`{slug}`: trailing text on heading: {trail.strip()!r}"))
        todo.issues += marker_issues(slug, path, line_no, body_lines)


def marker_issues(slug: str, path: str, line_no: int, body_lines: list[str]) -> list[Issue]:
    """In-band metadata warnings; `body_lines[0]` is at line `line_no + 1`."""
    return [Issue("warning", path, line_no + offset,
                  f"`{slug}`: in-band metadata marker {marker!r} in body")
            for offset, body_line in enumerate(body_lines, start=1)
            for marker in IN_BAND_MARKERS if marker in body_line]


def fragments(repo: Path) -> tuple[str | None, list[Entry], list[Issue]]:
    """Fragment directory, entries and issues, as `allthechanges --json todo query` reports.

    Runs nothing when the repository has no fragment directory.
    """
    present = fragment_dir_on_disk(repo)
    if present is None:
        return None, [], []
    if shutil.which("allthechanges") is None:
        raise TriageError(f"{present} exists but allthechanges is not on PATH; "
                          "install it with `cargo install allthechanges`")
    result = subprocess.run(["allthechanges", "--json", "todo", "query"], cwd=repo,
                            capture_output=True, text=True, check=False)
    report = query_report(result)
    if report is None:
        said = result.stderr.strip() or next(iter(result.stdout.strip().splitlines()),
                                             "(no output)")
        raise TriageError(f"allthechanges todo query failed (exit {result.returncode}): {said}")

    entries: list[Entry] = []
    issues: list[Issue] = []
    for item in report["fragments"]:
        path, slug, body = item["path"], item["slug"], item["body"]
        if not FRAGMENT_SLUG_RE.match(slug):
            issues.append(Issue("error", path, 0, f"bad-name: `{slug}` is not a lowercase slug"))
            continue
        entries.append(Entry(slug=slug, path=path, line=1, end_line=len(body), stop=0,
                             body="\n".join(body), heading=None, backticked=True, trail=""))
        issues += marker_issues(slug, path, 0, body)
    for problem in report["problems"]:
        issues.append(Issue("error", problem["path"], 0,
                            f"{problem['code']}: {problem['message']}"))
    return report["dir"], entries, issues


def fragment_dir_on_disk(repo: Path) -> str | None:
    """The first of `FRAGMENT_DIRS` present under `repo`, or None."""
    return next((name for name in FRAGMENT_DIRS if (repo / name).exists()), None)


def query_report(result: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
    """The report if the run exited 0 or 1 with one of the shape we read, else None."""
    if result.returncode not in (0, 1):
        return None
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(report, dict) or not {"dir", "fragments", "problems"} <= report.keys():
        return None
    if not (report["dir"] is None or isinstance(report["dir"], str)):
        return None
    fragments_ok = isinstance(report["fragments"], list) and all(
        isinstance(item, dict) and isinstance(item.get("path"), str)
        and isinstance(item.get("slug"), str) and isinstance(item.get("body"), list)
        and all(isinstance(line, str) for line in item["body"])
        for item in report["fragments"])
    problems_ok = isinstance(report["problems"], list) and all(
        isinstance(item, dict)
        and all(isinstance(item.get(key), str) for key in ("path", "code", "message"))
        for item in report["problems"])
    return report if fragments_ok and problems_ok else None


def read(repo: Path, todo_path: str) -> TodoFile:
    """`todo_path`, then the fragment directory's entries."""
    path = repo / todo_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if fragment_dir_on_disk(repo) is None:
            raise TriageError(f"{path} does not exist") from None
        todo = TodoFile(title=None)
    else:
        todo = parse(text, todo_path)
    fragment_dir, entries, issues = fragments(repo)
    todo.fragment_dir = fragment_dir
    in_file = todo.by_slug()
    for entry in entries:
        if entry.slug in in_file:
            issues.append(Issue("error", entry.path, 0,
                                f"`{entry.slug}` is also `{in_file[entry.slug].where}`"))
    todo.entries += entries
    todo.issues += issues
    return todo


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def first_seen(repo: Path, entry: Entry) -> str | None:
    """Date the entry first appeared in git, or None if uncommitted.

    A file entry: the oldest commit that changed how often its whole heading line
    occurs. A fragment: the oldest commit touching its file.
    """
    if entry.heading is None:
        result = git(repo, "log", "--format=%ad", "--date=short", "--", entry.path)
    else:
        # The whole line, newlines included, so `## foo` does not match `## foo-bar`.
        result = git(repo, "log", "--format=%ad", "--date=short", f"-S\n{entry.heading}\n",
                     "--", entry.path)
    dates = result.stdout.split()
    return dates[-1] if result.returncode == 0 and dates else None


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    text: str
    slug: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.text.strip()}"


def code_sites(repo: Path, todo_path: str) -> list[Site]:
    """Every `TODO(x)` token in tracked files other than the TODO file and fragments."""
    result = git(
        repo, "grep", "-z", "-n", "-I", "-E", r"TODO\([^()[:space:]]+\)", "--", ".",
        f":(exclude){todo_path}", *(f":(exclude){name}" for name in FRAGMENT_DIRS),
    )
    if result.returncode not in (0, 1):
        raise TriageError(f"git grep failed: {result.stderr.strip()}")
    sites: list[Site] = []
    # -z: `path NUL line NUL text`, with the path unquoted and free to contain `:`.
    for raw in result.stdout.splitlines():
        path, line, text = raw.split("\0", 2)
        for token in SITE_TOKEN_RE.findall(text):
            sites.append(Site(path=path, line=int(line), text=text, slug=token))
    return sites

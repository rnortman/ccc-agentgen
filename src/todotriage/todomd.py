"""The TODO.md grammar: a `# ` title, then `## `-headed entries to the next `## `."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import TriageError

HEADING_RE = re.compile(r"^## `?(?P<slug>[a-z0-9][a-z0-9_-]*)`?(?P<trail>.*)$")
ENTRY_START_RE = re.compile(r"^## ")
TITLE_RE = re.compile(r"^# \S")
SITE_TOKEN_RE = re.compile(r"TODO\(([^()\s]+)\)")

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
    line: int  # 1-based; 0 when the issue is not tied to a line
    message: str

    def render(self, todo_path: str) -> str:
        where = f"{todo_path}:{self.line}" if self.line else todo_path
        return f"{self.level}: {where}: {self.message}"


@dataclass(frozen=True)
class Entry:
    slug: str
    heading: str  # the full heading line, without its newline
    line: int  # 1-based line of the heading
    end_line: int  # 1-based last line of the body (== line when the body is empty)
    body: str
    backticked: bool
    trail: str

    @property
    def text(self) -> str:
        return self.heading + "\n" + self.body

    @property
    def body_hash(self) -> str:
        return body_hash(self.body)


@dataclass
class TodoFile:
    title: str | None
    entries: list[Entry] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def by_slug(self) -> dict[str, Entry]:
        """First entry per slug; duplicates are a lint error, so later ones lose."""
        out: dict[str, Entry] = {}
        for entry in self.entries:
            out.setdefault(entry.slug, entry)
        return out


def body_hash(body: str) -> str:
    """sha256 of the body with whitespace runs collapsed and the ends stripped."""
    normalised = re.sub(r"\s+", " ", body).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def parse(text: str) -> TodoFile:
    """Split into entries and collect structural issues; never raises on content."""
    lines = text.split("\n")
    todo = TodoFile(title=None)

    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None and TITLE_RE.match(lines[first]):
        todo.title = lines[first][2:].strip()
    else:
        line = first + 1 if first is not None else 0
        todo.issues.append(Issue("error", line, "file does not start with a `# ` title"))

    starts = [i for i, line in enumerate(lines) if ENTRY_START_RE.match(line)]
    seen: dict[str, int] = {}
    for index, start in enumerate(starts):
        stop = starts[index + 1] if index + 1 < len(starts) else len(lines)
        heading = lines[start]
        body_lines = lines[start + 1:stop]
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        body = "\n".join(body_lines)
        line_no = start + 1
        match = HEADING_RE.match(heading)
        if match is None:
            todo.issues.append(Issue("error", line_no, f"unparseable heading: {heading!r}"))
            continue
        slug = match["slug"]
        rest = heading[3:]
        backticked = rest.startswith("`") and rest[1 + len(slug):].startswith("`")
        trail = match["trail"]
        entry = Entry(
            slug=slug,
            heading=heading,
            line=line_no,
            end_line=line_no + len(body_lines),
            body=body,
            backticked=backticked,
            trail=trail,
        )
        todo.entries.append(entry)
        if slug in seen:
            todo.issues.append(
                Issue("error", line_no, f"duplicate slug `{slug}` (first at line {seen[slug]})")
            )
        else:
            seen[slug] = line_no
        if not body.strip():
            todo.issues.append(Issue("error", line_no, f"`{slug}` has an empty body"))
        if not backticked:
            todo.issues.append(Issue("warning", line_no, f"`{slug}`: slug is not backticked"))
        if trail.strip():
            todo.issues.append(
                Issue("warning", line_no, f"`{slug}`: trailing text on heading: {trail.strip()!r}")
            )
        for offset, body_line in enumerate(body_lines, start=1):
            for marker in IN_BAND_MARKERS:
                if marker in body_line:
                    todo.issues.append(
                        Issue("warning", line_no + offset,
                              f"`{slug}`: in-band metadata marker {marker!r} in body")
                    )
    return todo


def read(repo: Path, todo_path: str) -> TodoFile:
    path = repo / todo_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TriageError(f"{path} does not exist") from None
    return parse(text)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def first_seen(repo: Path, todo_path: str, heading: str) -> str | None:
    """Date of the oldest commit that added or removed `heading`, or None if uncommitted."""
    result = git(repo, "log", "--format=%ad", "--date=short", f"-S{heading}", "--", todo_path)
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
    """Every `TODO(x)` token in tracked files other than the TODO file itself."""
    result = git(
        repo, "grep", "-z", "-n", "-I", "-E", r"TODO\([^()[:space:]]+\)", "--", ".",
        f":(exclude){todo_path}",
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

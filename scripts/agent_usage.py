#!/usr/bin/env python3
"""Tally Claude Code token usage and API-equivalent cost per agent type.

Reads the session logs under ~/.claude/projects (ccc's per-profile config
dirs symlink their projects/ there, so one root covers every account).

Attribution:
  - A top-level session started with `claude --agent <type>` (how ccc runs
    one) carries an `agent-setting` line; its usage goes to `<type>`.
  - A top-level session without one is `(interactive)` when started from the
    terminal, `(sdk, no agent)` otherwise.
  - An in-process subagent (Task/Agent tool) lives in
    <session>/subagents/agent-*.jsonl beside a .meta.json naming its
    agentType; its usage goes to `task:<agentType>`.

Cost is what the tokens would cost at Anthropic first-party API list prices.
For a subscription login it is a proxy for each agent's share of quota, not
a bill: the logs do not record quota consumption.

Usage:
  scripts/agent_usage.py [--since 2026-11-01] [--until 2026-11-27]
                         [--project SUBSTR] [--source ccc [--by-variant]]
                         [--by agent,model] [--no-model] [--csv]
                         [--root DIR ...]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# $ per million tokens: input, output, cache read. Cache writes are 1.25x
# input (5-minute TTL) and 2x input (1-hour TTL) on every model.
# Matched by prefix, longest first, so "claude-opus-5-5" beats "claude-opus-5".
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-mythos-5-2": (8.00, 40.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-mythos-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-opus-4-5": (5.00, 25.00, 0.50),
    "claude-opus-4-1": (15.00, 75.00, 1.50),
    "claude-opus-4": (15.00, 75.00, 1.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-3-5-haiku": (0.80, 4.00, 0.08),
}
_PRICE_KEYS = sorted(PRICES, key=len, reverse=True)

# Fast mode multiplies the whole price list by 2 on the models that offer it.
FAST_MULTIPLIER = 2.0
# Haiku 5.5 charges 5x above a 100K-token prompt.
HAIKU55_LONG_PROMPT = 100_000
HAIKU55_LONG_MULTIPLIER = 5.0


def price_for(model: str) -> tuple[float, float, float] | None:
    for key in _PRICE_KEYS:
        if model.startswith(key):
            return PRICES[key]
    return None


@dataclass
class Tally:
    requests: int = 0
    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0
    cost: float = 0.0
    unpriced_requests: int = 0
    sessions: set[str] = field(default_factory=set)

    def add(self, other: Tally) -> None:
        self.requests += other.requests
        self.input += other.input
        self.cache_write_5m += other.cache_write_5m
        self.cache_write_1h += other.cache_write_1h
        self.cache_read += other.cache_read
        self.output += other.output
        self.cost += other.cost
        self.unpriced_requests += other.unpriced_requests
        self.sessions |= other.sessions


def request_cost(model: str, usage: dict) -> float | None:
    prices = price_for(model)
    if prices is None:
        return None
    p_in, p_out, p_read = prices
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    w5, w1 = cache_writes(usage)
    mult = 1.0
    if usage.get("speed") == "fast":
        mult *= FAST_MULTIPLIER
    if model.startswith("claude-haiku-5-5") and inp + read + w5 + w1 > HAIKU55_LONG_PROMPT:
        mult *= HAIKU55_LONG_MULTIPLIER
    dollars = (
        inp * p_in
        + w5 * p_in * 1.25
        + w1 * p_in * 2.0
        + read * p_read
        + out * p_out
    ) / 1e6
    return dollars * mult


def cache_writes(usage: dict) -> tuple[int, int]:
    total = usage.get("cache_creation_input_tokens") or 0
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        w5 = split.get("ephemeral_5m_input_tokens") or 0
        w1 = split.get("ephemeral_1h_input_tokens") or 0
        if w5 + w1 == total:
            return w5, w1
    # No TTL breakdown: assume the 5-minute price.
    return total, 0


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_date(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Scanner:
    def __init__(self, since: datetime | None, until: datetime | None) -> None:
        self.since = since
        self.until = until
        # (agent, model) -> Tally
        self.tallies: dict[tuple[str, str], Tally] = defaultdict(Tally)
        self.seen: set[tuple[str, str]] = set()
        self.unpriced_models: dict[str, int] = defaultdict(int)
        self.bad_lines = 0

    def scan_session_file(self, path: Path) -> None:
        """A top-level session: attribute by its agent-setting line."""
        agent = None
        entrypoint = None
        records = []
        with path.open("rb") as fh:
            for raw in fh:
                if b'"agent-setting"' in raw:
                    try:
                        agent = json.loads(raw).get("agentSetting") or agent
                    except ValueError:
                        self.bad_lines += 1
                    continue
                if b'"usage"' not in raw:
                    if entrypoint is None and b'"entrypoint"' in raw:
                        try:
                            entrypoint = json.loads(raw).get("entrypoint")
                        except ValueError:
                            pass
                    continue
                rec = self._usage_record(raw)
                if rec:
                    records.append(rec)
                    if entrypoint is None:
                        entrypoint = rec[4]
        if agent is None:
            agent = "(interactive)" if entrypoint == "cli" else "(sdk, no agent)"
        self._record(agent, path.stem, records)

    def scan_subagent_file(self, path: Path, session: str) -> None:
        meta = path.with_suffix(".meta.json")
        agent_type = None
        try:
            agent_type = json.loads(meta.read_text()).get("agentType")
        except (OSError, ValueError):
            pass
        agent = f"task:{agent_type or '(unknown)'}"
        records = []
        with path.open("rb") as fh:
            for raw in fh:
                if b'"usage"' in raw:
                    rec = self._usage_record(raw)
                    if rec:
                        records.append(rec)
        self._record(agent, session, records)

    def _usage_record(self, raw: bytes):
        try:
            d = json.loads(raw)
        except ValueError:
            self.bad_lines += 1
            return None
        if d.get("type") != "assistant":
            return None
        msg = d.get("message") or {}
        usage = msg.get("usage")
        model = msg.get("model") or "?"
        if not usage or model == "<synthetic>":
            return None
        ts = parse_ts(d.get("timestamp"))
        if self.since and ts and ts < self.since:
            return None
        if self.until and ts and ts >= self.until:
            return None
        key = (msg.get("id") or "", d.get("requestId") or d.get("uuid") or "")
        return key, model, usage, ts, d.get("entrypoint")

    def _record(self, agent: str, session: str, records) -> None:
        for key, model, usage, _ts, _ep in records:
            # One API response is logged once per content block, and resumed
            # or forked sessions copy earlier messages: count each id once.
            if key[0] and key in self.seen:
                continue
            self.seen.add(key)
            t = self.tallies[(agent, model)]
            t.requests += 1
            t.sessions.add(session)
            t.input += usage.get("input_tokens") or 0
            t.output += usage.get("output_tokens") or 0
            t.cache_read += usage.get("cache_read_input_tokens") or 0
            w5, w1 = cache_writes(usage)
            t.cache_write_5m += w5
            t.cache_write_1h += w1
            cost = request_cost(model, usage)
            if cost is None:
                t.unpriced_requests += 1
                self.unpriced_models[model] += 1
            else:
                t.cost += cost


def iter_projects(roots: list[Path], project_filter: str | None):
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for proj in sorted(root.iterdir()):
            real = os.path.realpath(proj)
            if real in seen or not proj.is_dir():
                continue
            seen.add(real)
            if project_filter and project_filter not in proj.name:
                continue
            yield proj


def scan(roots: list[Path], project_filter, since, until) -> Scanner:
    sc = Scanner(since, until)
    since_epoch = since.timestamp() if since else None
    for proj in iter_projects(roots, project_filter):
        for f in proj.glob("*.jsonl"):
            # A file last written before --since holds nothing newer.
            if since_epoch and f.stat().st_mtime < since_epoch:
                continue
            sc.scan_session_file(f)
        for f in proj.glob("*/subagents/*.jsonl"):
            if since_epoch and f.stat().st_mtime < since_epoch:
                continue
            sc.scan_subagent_file(f, f.parent.parent.name)
    return sc


def fmt_tokens(n: int) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


# ---- ccc audit log ---------------------------------------------------------

def _variant(agents_config: str | None) -> str:
    """configs/<variant>/<name>.toml -> "<variant>/<name>"; else the file name."""
    if not agents_config:
        return "(none)"
    p = Path(agents_config)
    if p.parent.parent.name == "configs":
        return f"{p.parent.name}/{p.stem}"
    return p.name


def scan_ccc(audit_dir: Path, by: list[str], since, until, project_filter):
    """One row per ccc invocation that finished, from its `end` record.

    Claude runs carry Claude Code's own cost (`native.modelUsage`), which
    covers the whole subprocess including its in-process subagents. Codex
    runs carry tokens only; their cost is left at 0 and flagged.
    """
    variants: dict[str, str] = {}
    index = audit_dir / "traces.jsonl"
    if index.exists():
        for raw in index.open("rb"):
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            variants[d.get("trace", "")] = _variant(d.get("agents_config"))
    grouped: dict[tuple[str, ...], Tally] = defaultdict(Tally)
    unpriced: dict[str, int] = defaultdict(int)
    for f in audit_dir.glob("*.jsonl"):
        if f.name == "traces.jsonl":
            continue
        trace = f.stem
        spawns: dict[str, dict] = {}
        starts: dict[str, dict] = {}
        for raw in f.open("rb"):
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            t = d.get("t")
            if t == "start":
                starts[d["inv"]] = d
            elif t == "spawn":
                spawns[d["inv"]] = d
            elif t == "end":
                ts = parse_ts(d.get("ts"))
                if (since and ts and ts < since) or (until and ts and ts >= until):
                    continue
                sp = spawns.get(d["inv"], {})
                if project_filter and project_filter not in (sp.get("cwd") or ""):
                    continue
                st = starts.get(d["inv"], {})
                harness = sp.get("harness") or "?"
                native = d.get("native") if isinstance(d.get("native"), dict) else {}
                agent = sp.get("agent") or st.get("agent") or f"({st.get('cmd') or '?'}, no agent)"
                base = {
                    "agent": agent,
                    "variant": variants.get(trace, "(unindexed)"),
                    "profile": sp.get("profile") or "(ambient)",
                    "harness": harness,
                }
                session = d.get("session") or sp.get("session") or d["inv"]
                model_usage = native.get("modelUsage")
                if isinstance(model_usage, dict) and model_usage:
                    for model, mu in model_usage.items():
                        dims = dict(base, model=mu.get("canonicalModel") or model)
                        tl = grouped[tuple(dims[x] for x in by)]
                        tl.requests += 1
                        tl.sessions.add(session)
                        tl.input += mu.get("inputTokens") or 0
                        tl.output += mu.get("outputTokens") or 0
                        tl.cache_read += mu.get("cacheReadInputTokens") or 0
                        tl.cache_write_5m += mu.get("cacheCreationInputTokens") or 0
                        tl.cost += mu.get("costUSD") or 0.0
                elif isinstance(native.get("usage"), dict):
                    u = native["usage"]
                    dims = dict(base, model=sp.get("model") or "?")
                    tl = grouped[tuple(dims[x] for x in by)]
                    tl.requests += 1
                    tl.sessions.add(session)
                    cached = u.get("cached_input_tokens") or 0
                    tl.input += (u.get("input_tokens") or 0) - cached
                    tl.cache_read += cached
                    tl.cache_write_5m += u.get("cache_write_input_tokens") or 0
                    tl.output += u.get("output_tokens") or 0
                    tl.unpriced_requests += 1
                    unpriced[f"{harness}:{dims['model']}"] += 1
    return grouped, unpriced


def rows(grouped: dict[tuple[str, ...], Tally]):
    total = sum(t.cost for t in grouped.values()) or 1.0
    return [(k, t, t.cost / total) for k, t in sorted(grouped.items(), key=lambda kv: -kv[1].cost)]


def group_transcripts(sc: Scanner, by: list[str]) -> dict[tuple[str, ...], Tally]:
    grouped: dict[tuple[str, ...], Tally] = defaultdict(Tally)
    for (agent, model), t in sc.tallies.items():
        dims = {"agent": agent, "model": model}
        grouped[tuple(dims[d] for d in by)].add(t)
    return grouped


def print_table(table, by: list[str], as_csv: bool) -> None:
    if as_csv:
        w = csv.writer(sys.stdout)
        w.writerow(by + ["sessions", "requests", "input", "cache_write", "cache_read", "output", "cost_usd", "share"])
        for key, t, share in table:
            w.writerow([*key, len(t.sessions), t.requests, t.input,
                        t.cache_write_5m + t.cache_write_1h, t.cache_read,
                        t.output, f"{t.cost:.4f}", f"{share:.4f}"])
        return
    label = " / ".join(by)
    label_w = max([len(" / ".join(k)) for k, _, _ in table] + [len(label), 5])
    hdr = (f"{label:<{label_w}}  {'sess':>6} {'reqs':>7} {'input':>8} {'c.write':>8} "
           f"{'c.read':>8} {'output':>8} {'cost $':>10} {'share':>6}")
    print(hdr)
    print("-" * len(hdr))
    grand = Tally()

    def line(name: str, t: Tally, tail: str) -> str:
        return (f"{name:<{label_w}}  {len(t.sessions):>6} {t.requests:>7} "
                f"{fmt_tokens(t.input):>8} {fmt_tokens(t.cache_write_5m + t.cache_write_1h):>8} "
                f"{fmt_tokens(t.cache_read):>8} {fmt_tokens(t.output):>8} {t.cost:>10.2f}{tail}")

    for key, t, share in table:
        grand.add(t)
        print(line(" / ".join(key), t, f"{'*' if t.unpriced_requests else ' '}{share:>6.1%}"))
    print("-" * len(hdr))
    print(line("TOTAL", grand, ""))


DIMS = {
    "transcripts": ["agent", "model"],
    "ccc": ["agent", "variant", "profile", "harness", "model"],
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=["transcripts", "ccc"], default="transcripts",
                    help="transcripts: Claude Code session logs (default); "
                         "ccc: ccc's audit log (adds variant, profile, Codex)")
    ap.add_argument("--by", default="agent,model",
                    help="comma-separated grouping: agent,model (transcripts); "
                         "agent,variant,profile,harness,model (ccc). Default: agent,model")
    ap.add_argument("--by-variant", action="store_true",
                    help="--source ccc only: add variant after agent in the grouping")
    ap.add_argument("--no-model", action="store_true", help="drop model from the grouping")
    ap.add_argument("--root", action="append", type=Path,
                    help="projects dir to scan (repeatable; default ~/.claude/projects)")
    ap.add_argument("--audit-dir", type=Path,
                    default=Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "ccc" / "audit",
                    help="ccc audit dir (default ~/.local/state/ccc/audit)")
    ap.add_argument("--since", type=parse_date, help="only usage at or after this ISO date/time (UTC unless offset given)")
    ap.add_argument("--until", type=parse_date, help="only usage before this ISO date/time")
    ap.add_argument("--project", help="transcripts: project dir name contains this; ccc: run cwd contains this")
    ap.add_argument("--csv", action="store_true", help="write CSV with raw token counts")
    args = ap.parse_args()

    by = [d.strip() for d in args.by.split(",") if d.strip()]
    if args.by_variant:
        if args.source != "ccc":
            ap.error("--by-variant needs --source ccc: session logs do not record the variant")
        if "variant" not in by:
            by.insert(by.index("agent") + 1 if "agent" in by else 0, "variant")
    if args.no_model and "model" in by:
        by.remove("model")
    bad = [d for d in by if d not in DIMS[args.source]]
    if bad:
        ap.error(f"--by {','.join(bad)} not available for --source {args.source}")

    if args.source == "ccc":
        grouped, unpriced = scan_ccc(args.audit_dir, by, args.since, args.until, args.project)
        bad_lines = 0
    else:
        roots = args.root or [Path.home() / ".claude" / "projects"]
        sc = scan(roots, args.project, args.since, args.until)
        grouped, unpriced, bad_lines = group_transcripts(sc, by), sc.unpriced_models, sc.bad_lines

    print_table(rows(grouped), by, args.csv)
    if unpriced:
        print("\n* rows include runs with no price, left out of cost:",
              ", ".join(f"{m} ({n})" for m, n in sorted(unpriced.items())), file=sys.stderr)
    if bad_lines:
        print(f"skipped {bad_lines} unparseable log lines", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

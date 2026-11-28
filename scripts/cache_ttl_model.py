#!/usr/bin/env python3
"""Estimate what each agent type would have cost under a 5-minute vs 1-hour prompt cache TTL.

Claude Code on a subscription caches the main conversation with a 1-hour TTL.
A 1-hour cache write costs 2x the input price; a 5-minute write costs 1.25x.
1-hour caching pays only when requests that share a prefix start 5-60 minutes
apart, e.g. a lead that waits on a background `ccc run` and wakes on its
task notification. This script replays the session logs under both TTLs.

Timing. Anthropic documents that a cache entry's lifetime is measured from the
*start* of the request that writes or reads it, and that each read refreshes
it; generation time counts against the lifetime. Each request's start is its
`api-request` record's timestamp, the client's send time. The gap before a
request is start-to-start from the previous request in its chain: one log
file, one model (caches are model-scoped). How the server times a queued
request is not documented. Measured on these logs (Nov 2026, requests
written with a 5-minute TTL), every gap up to 309s hit and every gap from
313s missed, so a 5-minute entry lasts about 310s by client clocks. The
5-minute estimate is a range around that edge: gaps up to 300s count as hits
(pessimistic) or up to 315s (optimistic).

Counterfactual, per request, from the logged token counts:
  - Each request should read about as much as the previous one cached:
    min(this prefix, previous prefix). It counts as an observed hit if it
    read at least half of that, after subtracting S, the reads on the
    chain's first request. S is the part another session kept warm (the shared
    system prompt and tools), which outlives this chain's gaps.
  - Observed hit, gap over the policy's TTL: the chain's own reads become
    writes; S stays a read.
  - Observed miss caused by the TTL (gap over the TTL the previous request
    wrote with) and the gap fits the policy's TTL: the expected reads come
    back out of the writes.
  - Every other miss (compaction, a changed prompt, a resumed session)
    happens under both policies.
  - All writes are priced at the policy's rate.

Not modeled: sibling sessions keeping a prefix warm beyond S, retries that
never logged a response (they refreshed the cache too, so gaps read long),
and quota. Costs are API list prices from agent_usage.py: for a
subscription they are a proxy for quota share, not a bill.

Usage:
  scripts/cache_ttl_model.py [--since 2026-11-01] [--until 2026-11-27]
                             [--project SUBSTR] [--by agent,model]
                             [--causes N] [--csv] [--root DIR ...]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_usage import (  # noqa: E402
    iter_projects,
    parse_date,
    parse_ts,
    request_cost,
    session_label,
    subagent_label,
)

TTL_SECONDS = {"5m": 300, "1h": 3600}
# The 5-minute counterfactual's hit cutoff, pessimistic and optimistic, in
# client-clock seconds (see the module docstring for the measured edge).
CUT_5M_LO = 300
CUT_5M_HI = 315
CUT_1H = 3600
# An observed read of at least this share of the expected reads is a hit.
HIT_FRACTION = 0.5

BUCKETS = [  # (label, upper bound in seconds, inclusive)
    ("<5m", CUT_5M_LO),
    ("~5m", CUT_5M_HI),
    ("5-60m", CUT_1H),
    (">60m", float("inf")),
]

CCC_RE = re.compile(r"(?:^|[\s;&|(/])ccc\s+(run|resume)\b")
TASK_TOOL_ID_RE = re.compile(r"<tool-use-id>([^<\s]+)</tool-use-id>")


@dataclass
class Req:
    start: datetime
    ttl: str | None  # TTL of the request's last cache breakpoint
    trigger: str  # what the request answered: a tool result, notification, user
    model: str = "?"
    usage: dict = field(default_factory=dict)
    key: tuple[str, str] = ("", "")

    @property
    def cached(self) -> int:
        u = self.usage
        return (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)


def tool_label(name: str, inp: dict) -> str:
    if name == "Bash":
        m = CCC_RE.search(inp.get("command") or "")
        return f"ccc {m.group(1)}" if m else "Bash"
    if name in ("Agent", "Task"):
        return f"Agent({inp.get('subagent_type') or 'default'})"
    return name


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def read_file(path: Path) -> tuple[list[Req], str | None, str | None, int]:
    """The file's requests that got a response, in order, plus attribution and the unanswered count."""
    reqs: list[Req] = []
    tools: dict[str, str] = {}  # tool_use id -> label
    file_seen: set[str] = set()
    pending: Req | None = None
    trigger = "first"
    agent_setting = entrypoint = None
    unanswered = 0
    with path.open("rb") as fh:
        for raw in fh:
            if b'"agent-setting"' in raw:
                try:
                    agent_setting = json.loads(raw).get("agentSetting") or agent_setting
                except ValueError:
                    pass
                continue
            if b'"type":"api-request"' in raw:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                ts = parse_ts(d.get("timestamp"))
                if ts is None:
                    continue
                if pending is not None:
                    unanswered += 1
                cc = d.get("cacheControl") or []
                ttl = (cc[-1].get("value") or {}).get("ttl", "5m") if cc else None
                pending = Req(ts, ttl, trigger, (d.get("params") or {}).get("model") or "?")
                trigger = "(no new input: retry, continuation)"
                continue
            if b'"type":"assistant"' in raw:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                msg = d.get("message") or {}
                if entrypoint is None:
                    entrypoint = d.get("entrypoint")
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tools[b.get("id", "")] = tool_label(b.get("name", "?"), b.get("input") or {})
                mid = msg.get("id") or ""
                if not mid or mid in file_seen:
                    continue
                file_seen.add(mid)
                if msg.get("model") == "<synthetic>" or not msg.get("usage") or pending is None:
                    continue
                pending.model = msg.get("model") or pending.model
                pending.usage = msg["usage"]
                pending.key = (mid, d.get("requestId") or d.get("uuid") or "")
                reqs.append(pending)
                pending = None
                continue
            if b'"type":"user"' in raw:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if d.get("type") != "user" or d.get("isMeta"):
                    continue
                if entrypoint is None:
                    entrypoint = d.get("entrypoint")
                content = (d.get("message") or {}).get("content")
                results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
                    if isinstance(content, list) else []
                if results:
                    names = sorted({tools.get(b.get("tool_use_id", ""), "?") for b in results})
                    trigger = "tool: " + ", ".join(names)
                    continue
                text = text_of(content).lstrip()
                if text.startswith("<task-notification>"):
                    m = TASK_TOOL_ID_RE.search(text)
                    trigger = "bg done: " + (tools.get(m.group(1), "?") if m else "?")
                elif text.startswith(("<command-", "<local-command")):
                    continue
                elif text:
                    trigger = "user message"
    if pending is not None:
        unanswered += 1
    return reqs, agent_setting, entrypoint, unanswered


@dataclass
class Row:
    requests: int = 0
    buckets: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    actual: float = 0.0
    cf_5m_lo: float = 0.0  # pessimistic 5-minute: hit up to CUT_5M_LO
    cf_5m_hi: float = 0.0  # optimistic 5-minute: hit up to CUT_5M_HI
    cf_1h: float = 0.0
    unpriced: int = 0


@dataclass
class Cause:
    n: int = 0
    gaps: list[float] = field(default_factory=list)
    saved_by_1h: float = 0.0  # 5-minute (pessimistic) cost minus 1-hour cost


def counterfactual(req: Req, prev: Req | None, shared: int, cutoff: float, write_ttl: str) -> dict:
    """req's usage as it would have been under a TTL whose hit cutoff is `cutoff` seconds."""
    u = req.usage
    read = u.get("cache_read_input_tokens") or 0
    write = u.get("cache_creation_input_tokens") or 0
    if prev is not None:
        gap = (req.start - prev.start).total_seconds()
        expected = min(req.cached, prev.cached)
        own_expected = expected - shared
        hit = own_expected <= 0 or (read - shared) >= HIT_FRACTION * own_expected
        if hit and gap > cutoff:
            keep = min(read, shared)
            write += read - keep
            read = keep
        elif not hit:
            ttl_caused = gap > TTL_SECONDS.get(prev.ttl or "1h", 3600)
            if ttl_caused and gap <= cutoff:
                extra = min(write, max(0, expected - read))
                read += extra
                write -= extra
    w5, w1 = (write, 0) if write_ttl == "5m" else (0, write)
    return dict(u, cache_read_input_tokens=read, cache_creation_input_tokens=write,
                cache_creation={"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1})


def bucket_of(gap: float) -> str:
    for label, upper in BUCKETS:
        if gap <= upper:
            return label
    return BUCKETS[-1][0]


def run(args) -> tuple[dict, dict, dict]:
    rows: dict[tuple[str, ...], Row] = defaultdict(Row)
    causes: dict[tuple[str, str], Cause] = defaultdict(Cause)
    stats = {"unanswered": 0, "files": 0}
    seen: set[tuple[str, str]] = set()
    roots = args.root or [Path.home() / ".claude" / "projects"]
    since_epoch = args.since.timestamp() if args.since else None
    files: list[tuple[Path, str | None]] = []
    for proj in iter_projects(roots, args.project):
        files += [(f, None) for f in proj.glob("*.jsonl")]
        files += [(f, subagent_label(f)) for f in proj.glob("*/subagents/*.jsonl")]
    for path, label in files:
        if since_epoch and path.stat().st_mtime < since_epoch:
            continue
        reqs, agent_setting, entrypoint, unanswered = read_file(path)
        stats["files"] += 1
        stats["unanswered"] += unanswered
        agent = label or session_label(agent_setting, entrypoint)
        last: dict[str, Req] = {}  # model -> previous request in the chain
        shared: dict[str, int] = {}  # model -> reads on the chain's first request
        for req in reqs:
            prev = last.get(req.model)
            if prev is None:
                shared[req.model] = req.usage.get("cache_read_input_tokens") or 0
            last[req.model] = req
            if req.key[0] and req.key in seen:
                continue  # a resumed session's copy of a response counted elsewhere
            seen.add(req.key)
            if (args.since and req.start < args.since) or (args.until and req.start >= args.until):
                continue
            s = shared[req.model]
            actual = request_cost(req.model, req.usage)
            lo = request_cost(req.model, counterfactual(req, prev, s, CUT_5M_LO, "5m"))
            hi = request_cost(req.model, counterfactual(req, prev, s, CUT_5M_HI, "5m"))
            h1 = request_cost(req.model, counterfactual(req, prev, s, CUT_1H, "1h"))
            dims = {"agent": agent, "model": req.model}
            row = rows[tuple(dims[d] for d in args.by)]
            row.requests += 1
            if prev is None:
                row.buckets["first"] += 1
                gap = None
            else:
                gap = (req.start - prev.start).total_seconds()
                row.buckets[bucket_of(gap)] += 1
            if None in (actual, lo, hi, h1):
                row.unpriced += 1
                continue
            row.actual += actual
            row.cf_5m_lo += lo
            row.cf_5m_hi += hi
            row.cf_1h += h1
            if gap is not None and CUT_5M_LO < gap <= CUT_1H:
                c = causes[(agent, req.trigger)]
                c.n += 1
                c.gaps.append(gap)
                c.saved_by_1h += lo - h1
    return rows, causes, stats


def verdict(row: Row) -> str:
    if row.cf_1h < row.cf_5m_hi:
        return "1h"
    if row.cf_1h > row.cf_5m_lo:
        return "5m"
    return "unclear"


def fmt_pct(x: float, base: float) -> str:
    return f"{x / base:+.0%}" if base else "-"


def print_tables(rows, causes, stats, by: list[str], n_causes: int) -> None:
    order = sorted(rows.items(), key=lambda kv: -kv[1].actual)
    label = " / ".join(by)
    w = max([len(" / ".join(k)) for k in rows] + [len(label), 5])
    blabels = ["first"] + [b for b, _ in BUCKETS]
    hdr = (f"{label:<{w}}  {'reqs':>6} " + " ".join(f"{b:>6}" for b in blabels)
           + f"  {'actual $':>9} {'1h $':>9} {'5m $ (range)':>19} {'5m vs 1h':>13}  best")
    print("Requests by start-to-start gap; cost actual and under each TTL for every request.\n")
    print(hdr)
    print("-" * len(hdr))
    tot = Row()
    for key, r in order:
        tot.requests += r.requests
        for b in blabels:
            tot.buckets[b] += r.buckets.get(b, 0)
        tot.actual += r.actual
        tot.cf_1h += r.cf_1h
        tot.cf_5m_lo += r.cf_5m_lo
        tot.cf_5m_hi += r.cf_5m_hi
        tot.unpriced += r.unpriced
        print(line(" / ".join(key), r, w, blabels))
    print("-" * len(hdr))
    print(line("TOTAL", tot, w, blabels))

    if n_causes:
        print(f"\nWhat ended the 5-60 minute gaps (where the TTL decides hit or miss), "
              f"top {n_causes} per agent by what 1h saved:\n")
        by_agent: dict[str, list[tuple[str, Cause]]] = defaultdict(list)
        for (agent, trig), c in causes.items():
            by_agent[agent].append((trig, c))
        aw = max([len(a) for a in by_agent] + [5])
        tw = max([len(t) for (_, t) in causes] + [7])
        h2 = f"{'agent':<{aw}}  {'trigger':<{tw}}  {'gaps':>6} {'median':>7}  {'1h saved $':>10}"
        print(h2)
        print("-" * len(h2))
        agents = sorted(by_agent, key=lambda a: -sum(c.saved_by_1h for _, c in by_agent[a]))
        for agent in agents:
            for trig, c in sorted(by_agent[agent], key=lambda tc: -tc[1].saved_by_1h)[:n_causes]:
                print(f"{agent:<{aw}}  {trig:<{tw}}  {c.n:>6} {median(c.gaps) / 60:>6.1f}m  {c.saved_by_1h:>10.2f}")

    print(f"\n{stats['files']} log files. {stats['unanswered']} requests logged no response "
          "(failed or interrupted) and are left out; each still refreshed the cache, "
          "so some gaps read long.", file=sys.stderr)
    if tot.unpriced:
        print(f"{tot.unpriced} requests on unpriced models are counted in reqs but not in cost.",
              file=sys.stderr)


def line(name: str, r: Row, w: int, blabels: list[str]) -> str:
    lo_d = r.cf_5m_lo - r.cf_1h
    hi_d = r.cf_5m_hi - r.cf_1h
    rng = f"{r.cf_5m_hi:.2f}-{r.cf_5m_lo:.2f}"
    delta = f"{fmt_pct(hi_d, r.cf_1h)}..{fmt_pct(lo_d, r.cf_1h)}"
    return (f"{name:<{w}}  {r.requests:>6} " + " ".join(f"{r.buckets.get(b, 0):>6}" for b in blabels)
            + f"  {r.actual:>9.2f} {r.cf_1h:>9.2f} {rng:>19} {delta:>13}  {verdict(r)}")


def write_csv(rows, by: list[str]) -> None:
    w = csv.writer(sys.stdout)
    blabels = ["first"] + [b for b, _ in BUCKETS]
    w.writerow(by + ["requests"] + [f"gap_{b}" for b in blabels]
               + ["actual_usd", "ttl_1h_usd", "ttl_5m_optimistic_usd", "ttl_5m_pessimistic_usd", "best"])
    for key, r in sorted(rows.items(), key=lambda kv: -kv[1].actual):
        w.writerow([*key, r.requests] + [r.buckets.get(b, 0) for b in blabels]
                   + [f"{r.actual:.4f}", f"{r.cf_1h:.4f}", f"{r.cf_5m_hi:.4f}", f"{r.cf_5m_lo:.4f}", verdict(r)])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--by", default="agent", help="comma-separated grouping: agent,model. Default: agent")
    ap.add_argument("--root", action="append", type=Path,
                    help="projects dir to scan (repeatable; default ~/.claude/projects)")
    ap.add_argument("--since", type=parse_date, help="only requests starting at or after this ISO date/time")
    ap.add_argument("--until", type=parse_date, help="only requests starting before this ISO date/time")
    ap.add_argument("--project", help="project dir name contains this")
    ap.add_argument("--causes", type=int, default=5, metavar="N",
                    help="show the top N gap triggers per agent (0 to hide). Default: 5")
    ap.add_argument("--csv", action="store_true", help="write the per-group table as CSV")
    args = ap.parse_args()
    args.by = [d.strip() for d in args.by.split(",") if d.strip()]
    bad = [d for d in args.by if d not in ("agent", "model")]
    if bad:
        ap.error(f"--by {','.join(bad)}: choose from agent, model")
    rows, causes, stats = run(args)
    if args.csv:
        write_csv(rows, args.by)
    else:
        print_tables(rows, causes, stats, args.by, args.causes)
    return 0


if __name__ == "__main__":
    sys.exit(main())

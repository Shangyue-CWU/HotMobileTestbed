#!/usr/bin/env python3
"""One plain-text report per run, for sharing through git (results/ itself is git-ignored).

  python3 report.py results/quick            # writes results/quick/report.txt and prints it

Contains: machine, self-test, summary table, and the first-run checks from AI_CONTEXT.md section 7
(TLP read-back, flows_active, failures, DualPI2 stats key names and delay units).
"""
from __future__ import annotations

import collections
import glob
import json
import os
import sys


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def section(out, title, body):
    out.append(f"\n===== {title} =====\n{body.rstrip() if body else '(missing)'}\n")


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    out = [f"Rescue-lane testbed report: {os.path.abspath(d)}"]

    metas = sorted(glob.glob(os.path.join(d, "*.meta.json")))
    if metas:
        m = json.loads(read(metas[0]))
        out.append(f"host {m.get('host')}  kernel {m.get('kernel')}")
        section(out, "sysctl (srv) at start", m.get("sysctl_srv"))
        section(out, "bottleneck qdisc at start", m.get("qdisc"))

    for p in sorted(glob.glob(os.path.join(d, "selftest_*.txt"))):
        section(out, os.path.basename(p), read(p))

    section(out, "summary.md", read(os.path.join(d, "figs", "summary.md")))

    for p in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
        if p.endswith(".qmon.jsonl"):
            continue
        rows = load(p)
        name = os.path.basename(p)
        lines = [f"trials: {len(rows)}"]

        c = collections.Counter((r["tlp"], json.dumps(r.get("sysctl"), sort_keys=True)) for r in rows)
        lines.append("TLP setting -> effective sysctls (trials):")
        lines += [f"  tlp={t!s:5s} {s}  ({n})" for (t, s), n in sorted(c.items())]

        fa = collections.Counter(r.get("flows_active") for r in rows)
        want = rows[0]["bg"] + 1 if rows else None
        lines.append(f"flows_active (want {want}): {dict(fa)}")

        fails = collections.Counter((r["variant"], r.get("error")) for r in rows if not r["ok"])
        lines.append(f"failures: {sum(fails.values())}" + (f"  {dict(fails)}" if fails else ""))
        section(out, f"checks: {name}", "\n".join(lines))

        q = p[:-len(".jsonl")] + ".qmon.jsonl"
        if os.path.exists(q):
            qs = [s.get("q") or {} for s in load(q)]
            qs = [s for s in qs if s]
            body = [f"samples: {len(qs)}"]
            if qs:
                body.append("keys: " + ", ".join(sorted(qs[0])))
                for k in ("delay_c", "delay_l"):
                    v = [s[k] for s in qs if isinstance(s.get(k), (int, float))]
                    if v:
                        body.append(f"{k} (raw units): p50 {pct(v, .5)}  p90 {pct(v, .9)}  "
                                    f"p99 {pct(v, .99)}  max {max(v)}")
                busiest = max(qs, key=lambda s: s.get("delay_c", 0) if isinstance(s.get("delay_c"), (int, float)) else 0)
                body.append("sample with the largest delay_c:\n" + json.dumps(busiest, indent=1))
            section(out, f"queue monitor: {os.path.basename(q)}", "\n".join(body))

        for log in (p[:-len(".jsonl")] + ".serve.log", p[:-len(".jsonl")] + ".qmon.log"):
            txt = read(log)
            if txt and txt.strip():
                section(out, os.path.basename(log) + " (last 30 lines)", "\n".join(txt.splitlines()[-30:]))

    text = "\n".join(out) + "\n"
    with open(os.path.join(d, "report.txt"), "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()

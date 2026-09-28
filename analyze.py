#!/usr/bin/env python3
"""Summarise rescue-lane testbed results.

  python3 analyze.py results/dualpi2_step.jsonl [more.jsonl ...] --out figs/

Writes summary.md (tables) and, if matplotlib is installed, rescue_cdf.{png,pdf}
and queue_transient.{png,pdf}.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict

ORDER = ["l4s", "ecn", "classic", "fresh", "abandon", "dctcp_c"]
LABEL = {
    "l4s": "L4S lane (DCTCP, L queue)",
    "ecn": "ECN lane (Cubic, C queue)",
    "classic": "Classic lane (Cubic)",
    "fresh": "New connection",
    "abandon": "Abandon + refetch",
    "dctcp_c": "DCTCP in C queue",
}
COLOR = {"l4s": "#1f77b4", "ecn": "#17becf", "classic": "#7f7f7f",
         "fresh": "#ff7f0e", "abandon": "#d62728", "dctcp_c": "#9467bd"}
DEADLINES = (0.5, 1.0, 1.5, 2.0)


def load(paths):
    rows = []
    for p in paths:
        with open(p) as f:
            rows += [json.loads(line) for line in f if line.strip()]
    return rows


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return math.nan
    k = min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))
    return xs[k]


def wilson(k, n, z=1.96):
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def fmt(x):
    return "inf" if x == math.inf else ("-" if x != x else f"{x:.2f}")


def summarise(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r["aqm"], r["event"], r["tlp"], r["variant"])].append(r)
    lines = []
    for (aqm, event) in sorted({k[:2] for k in groups}):
        lines += [f"\n## AQM={aqm}, event={event}\n",
                  "| TLP | rescue path | n | fail % | p50 s | p90 s | p99 s | "
                  + " | ".join(f"in time <{d}s (95% CI)" for d in DEADLINES) + " | retrans/rescue |",
                  "|---|---|---|---|---|---|---|" + "---|" * len(DEADLINES) + "---|"]
        for tlp in (True, False):
            for v in ORDER:
                g = groups.get((aqm, event, tlp, v))
                if not g:
                    continue
                t = [r["completion_s"] if r["ok"] else math.inf for r in g]
                n = len(t)
                fails = sum(1 for r in g if not r["ok"])
                cells = []
                for d in DEADLINES:
                    k = sum(1 for x in t if x < d)
                    lo, hi = wilson(k, n)
                    cells.append(f"{k / n:.0%} ({lo:.0%}–{hi:.0%})")
                rt = [r["retrans"] for r in g if "retrans" in r]
                rt_s = f"{sum(rt) / len(rt):.2f}" if rt else "-"
                lines.append(f"| {'on' if tlp else 'off'} | {LABEL[v]} | {n} | {100 * fails / n:.0f} | "
                             f"{fmt(pct(t, .5))} | {fmt(pct(t, .9))} | {fmt(pct(t, .99))} | "
                             + " | ".join(cells) + f" | {rt_s} |")
    return "\n".join(lines)


def plot_cdf(rows, out, plt):
    combos = sorted({(r["aqm"], r["event"]) for r in rows})
    for aqm, event in combos:
        sub = [r for r in rows if r["aqm"] == aqm and r["event"] == event]
        tlps = [t for t in (True, False) if any(r["tlp"] == t for r in sub)]
        fig, axes = plt.subplots(1, len(tlps), figsize=(3.4 * len(tlps), 2.6), sharey=True, squeeze=False)
        for ax, tlp in zip(axes[0], tlps):
            for v in ORDER:
                g = [r for r in sub if r["tlp"] == tlp and r["variant"] == v]
                if not g:
                    continue
                t = sorted(r["completion_s"] for r in g if r["ok"])
                y = [(i + 1) / len(g) for i in range(len(t))]     # failures keep the curve below 1
                ax.step(t, y, where="post", color=COLOR[v], lw=1.6, label=LABEL[v])
            ax.set_xscale("log")
            ax.set_xlim(0.05, 5)
            ax.set_ylim(0, 1.02)
            ax.set_xlabel("rescue completion time (s)")
            ax.set_title(f"TLP/RACK {'on (Linux default)' if tlp else 'off (ns-3-like)'}", fontsize=9)
            ax.grid(alpha=0.3, lw=0.5)
            for s in ("top", "right"):
                ax.spines[s].set_visible(False)
        axes[0][0].set_ylabel("fraction of rescues")
        axes[0][-1].legend(fontsize=7, frameon=False, loc="lower right")
        fig.tight_layout()
        for ext in ("png", "pdf"):
            fig.savefig(os.path.join(out, f"rescue_cdf_{aqm}_{event}.{ext}"), dpi=200)
        plt.close(fig)


def queue_delays(q):
    """(classic ms, L4S ms) from one dualpi2 stats object, else backlog-derived single value."""
    if not q:
        return None
    q = {k.replace("-", "_"): v for k, v in q.items()}    # older qmon files kept tc's hyphens
    if "delay_c" in q:
        return q["delay_c"] / 1000.0, q.get("delay_l", 0) / 1000.0
    return None


def plot_queue(rows, qmon_paths, out, plt, window=(-2.0, 8.0), bin_s=0.05):
    samples = []
    for p in qmon_paths:
        with open(p) as f:
            for line in f:
                s = json.loads(line)
                d = queue_delays(s.get("q"))
                if d:
                    samples.append((s["t"], *d))
    if not samples:
        print("queue plot skipped: no dualpi2 delay_c/delay_l fields in qmon logs")
        return
    samples.sort()
    ts = [s[0] for s in samples]
    import bisect
    for event in sorted({r["event"] for r in rows}):
        t0s = [r["t0"] for r in rows if r["event"] == event and "t0" in r and r["aqm"] == "dualpi2"]
        nb = int((window[1] - window[0]) / bin_s)
        bins_c, bins_l = [[] for _ in range(nb)], [[] for _ in range(nb)]
        for t0 in t0s:
            i = bisect.bisect_left(ts, t0 + window[0])
            while i < len(samples) and samples[i][0] < t0 + window[1]:
                b = int((samples[i][0] - t0 - window[0]) / bin_s)
                if 0 <= b < nb:
                    bins_c[b].append(samples[i][1])
                    bins_l[b].append(samples[i][2])
                i += 1
        x = [window[0] + (b + 0.5) * bin_s for b in range(nb)]
        med = lambda xs: pct(xs, .5) if xs else math.nan
        fig, ax = plt.subplots(figsize=(5, 2.4))
        ax.plot(x, [med(b) for b in bins_c], color="#d62728", lw=1.4, label="classic queue (median)")
        ax.plot(x, [pct(b, .9) if b else math.nan for b in bins_c], color="#d62728", lw=0.8, ls=":",
                label="classic queue (p90)")
        ax.plot(x, [med(b) for b in bins_l], color="#1f77b4", lw=1.4, label="L4S queue (median)")
        ax.plot(x, [pct(b, .9) if b else math.nan for b in bins_l], color="#1f77b4", lw=0.8, ls=":",
                label="L4S queue (p90)")
        ax.axvline(0, color="k", lw=0.6)
        ax.set_yscale("symlog", linthresh=1)
        ax.set_xlabel(f"time since {'capacity step' if event == 'step' else 'traffic onset'} (s)")
        ax.set_ylabel("queue delay (ms)")
        ax.legend(fontsize=7, frameon=False, ncol=2)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.set_title(f"DualPI2, {len(t0s)} events", fontsize=9)
        fig.tight_layout()
        for ext in ("png", "pdf"):
            fig.savefig(os.path.join(out, f"queue_transient_{event}.{ext}"), dpi=200)
        plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("results", nargs="+")
    p.add_argument("--out", default="figs")
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    # run_all.sh passes "$OUT"/*.jsonl, which also matches the queue-monitor logs: keep trial files only
    a.results = [p for p in a.results if not p.endswith(".qmon.jsonl")]
    rows = load(a.results)
    bad = [r for r in rows if "flows_active" in r and r["flows_active"] != r["bg"] + 1]
    if bad:
        print(f"WARNING: {len(bad)} of {len(rows)} trials had fewer long flows than configured "
              f"(cross traffic did not start); check the .serve.log files")
    table = summarise(rows)
    with open(os.path.join(a.out, "summary.md"), "w") as f:
        f.write("# Rescue-lane testbed summary\n" + table + "\n")
    print(table)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: tables only")
        return
    plot_cdf(rows, a.out, plt)
    qmon = [r[:-len(".jsonl")] + ".qmon.jsonl" for r in a.results]
    plot_queue(rows, [q for q in qmon if os.path.exists(q)], a.out, plt)
    print(f"figures in {a.out}/")


if __name__ == "__main__":
    main()

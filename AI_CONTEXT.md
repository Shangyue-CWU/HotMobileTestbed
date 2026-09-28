# Context for AI assistants working on this testbed

Read this first. It explains why this testbed exists, what it measures, what has been verified, and what must not be changed casually. Last updated: Sep 28, 2026.

## 1. Background

- **The paper.** A HotMobile '27 submission (deadline Oct 9, 2026, 11:59 pm AoE). It asks whether L4S lets a short-video player keep a smaller playback buffer, and so waste fewer bytes when users swipe, without more stalls.
- **The proposed mechanism ("L4S-Bail", policy P2).** The player keeps a small buffer on a normal best-effort (BE) connection. When a stall is imminent, it fetches the needed chunk at the lowest quality over a second, L4S connection (the "rescue lane").
- **Who does what.** The main evaluation is done in ns-3 by the PI's own pipeline, which lives elsewhere, not in this repo.
- **Why this testbed exists.** The ns-3 draft explains the lane's gain as a loss effect: a small rescue fetch (about 100 KB) that loses a segment waits for a retransmission timeout, and the ECN/L4S lane avoids that loss. ns-3's TCP has no RACK-TLP; its model documentation says "there is no RACK-TLP or similar mechanism". Linux enables tail-loss probing by default. So the ns-3 gain may be partly a simulator artifact.

## 2. The one question this testbed answers

On a real Linux stack with mainline DualPI2, does a rescue fetch over the L4S lane finish in time more often than over:
- a classic second connection,
- a classic-ECN second connection,
- a new connection,
- abandon + refetch?

Each is tested with TLP on (Linux default) and TLP off (closer to ns-3).

The testbed does not measure player stalls, waste, bitrate, Wi-Fi, or cellular. Do not extend claims beyond rescue-fetch completion time, failure rate, and queue delay around a transient.

## 3. How to read the outcome (decided in advance)

Compare paths with TLP on:
- **A.** L4S lane clearly better than classic, fresh and abandon: the L4S claim holds on a real stack.
- **B.** L4S about equal to the ECN lane, both better than classic: the claim becomes "a rescue path that avoids loss"; L4S is the deployable version.
- **C.** Fresh or abandon about equal to L4S: the ns-3 gap came mostly from missing TLP.

Report whichever occurs. Do not tune parameters after seeing results to move the outcome.

## 4. Design

**Topology** (`topo.sh`): three network namespaces on one Linux host.

```
srv (10.0.1.1/.2/.3) -- r0 [rtr] r1 -- cli (10.0.2.1)
```

- **Bottleneck:** `rtr` egress `r1`, with an HTB shaper (class 1:10) whose rate is changed at run time. The leaf qdisc (handle 20:) is the AQM: `dualpi2` at Linux defaults, `bfifo` sized to 250 ms, or `fq_codel`.
- **Base RTT:** 30 ms of netem on `r0` (the rtr to srv direction). The bottleneck direction therefore holds only the AQM queue.
- **HTB burst:** set to about 1 ms of data (minimum 3000 B), both in `topo.sh` and on every rate change in `tb.py`. Larger bursts would trip DualPI2's 1 ms L-queue step threshold.
- **Offloads:** GRO/GSO/TSO off on all veths.

**Lane selection by server address:**

| Address | Lane | Congestion control | Queue |
|---|---|---|---|
| 10.0.1.1 | BE and classic lane | Cubic, Not-ECT | C |
| 10.0.1.2 | ECN lane | Cubic, ECT(0) | C, marked |
| 10.0.1.3 | L4S lane | DCTCP | L |

- The client asks for ECN only toward `.2` and `.3`, using the per-route `features ecn`.
- Mainline DCTCP sends ECT(0), not ECT(1). The L4S lane therefore reaches the L queue through an nft rule on `rtr` that sets skb priority 20:1, which DualPI2 honours as L4S. The rule matches only ECN-capable packets from `.3`, so Not-ECT control packets (SYN-ACK, pure ACKs) stay in the C queue.

**One trial** (`tb.py trial()`):
1. Set the rate to 50 Mbit/s.
2. Start 5 Cubic bulk flows plus the video's BE bulk flow.
3. For persistent lanes, open the lane and warm it with a 20 KB fetch. There is no canary.
4. Wait 6 s. Record `flows_active`, which should be 6.
5. Trigger the event: `step` (50 to 8 Mbit/s) or `onset` (+5 bulk flows).
6. After a delay of 0.1 or 0.5 s, fetch 100 KB over the chosen path, with a 5 s deadline.
7. Record completion time, errors, retransmissions, cwnd and srtt, and the effective TLP sysctls.
8. Tear down, restore the rate, and wait 2 s.

Trials are shuffled with a fixed seed.

**Rescue paths (variants):**
- `l4s`, `ecn`, `classic`: persistent second connection.
- `fresh`: new Cubic connection; BE keeps running.
- `abandon`: reset BE, then refetch on a new connection.
- `dctcp_c`: DCTCP left in the C queue; optional, not in the default run.

**TLP on/off:** set in the `srv` namespace (the data sender) through `tcp_early_retrans` (3 or 0) and `tcp_recovery` (1 or 0). The values are read back after every change:
- the run stops if `tcp_early_retrans` did not change;
- a note is printed if the kernel kept RACK on;
- the effective values are stored per trial under `sysctl`.

## 5. Files

| File | Purpose |
|---|---|
| `preflight.sh` | Checks root, kernel >= 6.17, dualpi2 support, dctcp, nft/iptables, tools |
| `vm.sh` | Host-side helper: Ubuntu 26.04 multipass VM for hosts without dualpi2 (`up` / `selftest` / `quick` / `full` / `push` / `shell` / `pull` / `delete`); run commands write `Linux_selftest.txt`, `Linux_results_<name>.txt` and `Linux_run_<name>.log` in the repo root for committing |
| `report.py` | One plain-text report per run (`results/<name>/report.txt`): self-test, summary, TLP read-back, flows_active, failures, DualPI2 stats keys and raw delay values, child-process logs |
| `topo.sh` | `up` / `down` / `show` for the namespaces and qdiscs (env: `AQM`, `RATE`, `RTT_MS`, `FIFO_MS`) |
| `tb.py` | `serve` (srv ns), `qmon` (rtr ns, polls `tc -s -j qdisc` every 20 ms), `selftest` and `rescue` (cli ns; they spawn serve/qmon) |
| `run_all.sh` | Full campaign (DualPI2 step, DualPI2 onset, FIFO step), or `QUICK=1` smoke run |
| `analyze.py` | `summary.md` (fail %, percentiles, in-time rates with Wilson CIs), CDF and queue plots |
| `README.md` | User-facing description |
| `RUN_COMMANDS.md` | Copy-paste commands: Mac to Linux box and back |

Outputs per run, in `results/<name>/`:
- `<aqm>_<event>.jsonl`: one line per trial.
- `.qmon.jsonl`: queue stats.
- `.meta.json`: kernel, sysctls, qdisc config, args.
- `.serve.log`, `.qmon.log`: stderr of the child processes.
- `selftest_dualpi2.txt`.
- `figs/`.

The paper figures are built elsewhere (`../figures/from_testbed.py` converts these results).

## 6. Status and what has been verified

- **Sep 27:** written. It passed `py_compile` and `bash -n`. `analyze.py` was checked on synthetic data.
- **Sep 27, review fixes:** HTB burst, TLP read-back, 20 reps and 6 s warm-up, child stderr to log files, 2 s deadline added to the summary, `.gitignore`.
- **Sep 28, review fixes:**
  - The run now waits for the server and stops child processes cleanly. Before, the selftest server could still hold the ports when the rescue run started its own.
  - `flows_active` is recorded per trial, with a warning in the console and in `analyze.py` if cross traffic did not start.
  - L-queue steering is limited to ECN-capable packets.
  - `preflight.sh` loads `dummy` and `sch_dualpi2`.
  - `topo.sh show` prints the nft ruleset.
- **Sep 28, loopback check on macOS** (Linux-only calls stubbed out): all five variants and both events completed, cross traffic started, and rate changes fired in order.
- **Sep 28, local Linux host has no DualPI2.** The host is the CUDA box: Ubuntu 24.04, kernel 6.8.0-106, iproute2 6.1, NVIDIA 550 via DKMS. It has no `sch_dualpi2`, and its `tc` does not know `dualpi2`. The kernel is not swapped (NVIDIA DKMS). Instead, `vm.sh` runs the testbed in an Ubuntu 26.04 VM (kernel 7.0, iproute2 6.19). `preflight.sh` now says whether the kernel or `tc` is missing dualpi2. The AQM is unchanged: do not substitute fq_pie, cake or similar for dualpi2.
- **Sep 28, `vm.sh up` failed with "instance 'l4s-tb' does not exist".** Cause: the cloud-init file was made with `mktemp` in `/tmp`, which snap multipass cannot see (snaps have a private `/tmp`), so `multipass launch` failed before the VM existed. The file is now `vm-cloud-init.yaml` in the repo (must be under `$HOME`). `push`/`shell`/`pull` now stop with a clear message if the VM does not exist, and `up` reuses an existing VM.
- **Sep 28, VM works.** After the fix and a one-time `multipass authenticate`, `./vm.sh up` launched `l4s-tb`: kernel 7.0.0-31-generic, iproute2 6.19.0, Python 3.14.4, preflight passed on every line (dualpi2, dctcp, nft, matplotlib).
- **Sep 28, results transport.** `results/` is git-ignored and lives inside the VM until pulled, so the user saw an empty host folder. Added `report.py` (run by `run_all.sh`) and `vm.sh selftest|quick|full`, which run from the host, pull automatically and write the `Linux_*.txt` files above. To read results on the Mac: `git pull` in this repo, then read those files.
- **Sep 28, first self-test in the VM (`Linux_selftest.txt`).** Steering and ECN work, judged from the DualPI2 counters of one 3 MB fetch per lane:

  | Lane | pkts in C | pkts in L | CE marks | drops |
  |---|---|---|---|---|
  | classic | 2078 | 1 | 0 | 3 |
  | ecn | 2078 | 0 | 2 | 0 |
  | l4s | 2 | 2074 | 66 (all step) | 0 |

  The 2 C packets on the l4s lane are the Not-ECT handshake packets, as intended by the `ip ecn != not-ect` rule. The 1 L packet during the classic fetch is unexplained but negligible (first connection on a fresh topology). Qdisc config matches the plan (target 15 ms, step 1 ms, coupling 2, HTB burst 6250 B = 1 ms). The HTB "quantum is big" warning is harmless with a single class.
  Two reader bugs found and fixed: (1) `tc -j` spells DualPI2 stats with hyphens (`pkts-in-l`, `ecn-mark`, and presumably `delay-c`/`delay-l`), so selftest printed `L-queue packets=None` and the queue plot / Fig. 3 converter would have found no delay data; `tb.py` now normalizes keys to underscores (`norm()`), and analyze.py, report.py and from_testbed.py accept both spellings. (2) The `ss` ECN check printed False for all three lanes, contradicted by the counters (marks without drops); selftest now prints the raw `ss` line and CE marks/drops so the cause is visible. Delay units still unverified (assumed µs).
- **Sep 28, quick run `quick_0928_1552` (DualPI2, step, 3 reps = 60 trials, ~10 min).** Pipeline checks from section 7 all pass: 0 failures; `flows_active` = 6 in 60/60; TLP off really off (`tcp_early_retrans=0` AND `tcp_recovery=0` accepted, so RACK is off too); DualPI2 JSON `delay_c`/`delay_l` are in **microseconds** (the `target` option reads 15000 for 15 ms; classic delay p50 12.6 ms, p99 141 ms, max 206 ms; L delay p99 0, max 12 ms), so the /1000 in analyze.py and from_testbed.py is right. Bug: `run_all.sh` passes `"$OUT"/*.jsonl`, which also matched `*.qmon.jsonl`, so analyze.py crashed (`KeyError: 'aqm'`) and there was no summary.md; fixed (analyze.py drops qmon files). Selftest mark count double-counted (`step_mark` is a subset of `ecn_mark`); fixed. `ss` in this VM prints no option flags at all (not even ts/sack), so the ss ECN check is useless here; the queue counters are the evidence. Completion times (n=6 per path and TLP setting, smoke-test only, NOT evidence for outcome A/B/C): all paths have medians of 1.0 to 1.8 s, and every rescue finished by 2.2 s.
- **Rescue trials: only the quick run so far.** Nothing about DualPI2 behavior, ECN negotiation or steering has been observed yet.

## 7. Things to verify on the first Linux run

1. **`selftest` output.**

   | Path | ECN negotiated | L-queue packets |
   |---|---|---|
   | classic | no | 0 |
   | ecn | yes | 0 |
   | l4s | yes | > 0 |

   The DualPI2 JSON key for L-queue packets is assumed to be `pkts_in_l`; selftest prints every changed counter, so check the actual names there.
2. **The nft rule was accepted.** `topo.sh up` would fail loudly if the `ip ecn != not-ect` syntax is rejected; the iptables fallback uses `-m ecn ! --ecn-ip-ect 0`.
3. **Units of DualPI2 `delay_c` / `delay_l`** in `tc -j` output. The code assumes microseconds and divides by 1000 to get ms. Compare with `tc -s qdisc show dev r1` text output.
4. **TLP really switched off in `tlp=false` trials** (the `sysctl` field in each record). `tcp_recovery` may stay at 1 on recent kernels. That is recorded, not an error.
5. **Every trial has `flows_active == 6`.**

## 8. Rules for changes

- **Keep the defaults fixed within a campaign:** 50 to 8 Mbit/s, 30 ms, 5 background flows, 100 KB, 0.1 and 0.5 s delays, 20 reps, seed 1. If something must change, start a new `OUT` directory and record the change in `../testing_record.md` (local only).
- **Result files are appended.** Never reuse an `OUT` directory for a different configuration.
- **Keep variant names stable.** `../figures/from_testbed.py` maps `l4s`, `ecn`, `classic`, `fresh`, `abandon` to the paper's policy names.
- **Do not add a canary** or keep-alive traffic to the lane. The paper discussion found it adds overhead without reducing stalls.
- **The PI is sensitive to AI-written prose.** Report results as numbers and tables, not polished text.
- **Experiments run on the user's Linux machine,** kernel 6.17 or later, as root: on the CUDA box that means inside the `vm.sh` VM, not on the host. Do not try to run them on the user's Mac.

## 9. Known limitations (state them whenever results are quoted)

- DCTCP instead of Prague, with no pacing, so the initial window can be step-marked.
- ECT(0) steered by skb priority, not ECT(1) classification.
- Wired veth link: no Wi-Fi MAC, no cellular RLC.
- One object per trial; no player loop.
- Python application with about 1 ms timing resolution.

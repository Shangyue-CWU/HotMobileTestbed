# L4S rescue-lane testbed (Linux)

This testbed is an independent check of the ns-3 results. It uses the Linux TCP stack and the mainline DualPI2 qdisc in three network namespaces on one Linux machine. It needs no extra hardware and no ns-3.

It reproduces the core mechanism of the paper, not the full player:
1. The video's best-effort (BE) connection is busy.
2. The bottleneck goes through a transient: either a capacity drop, or other devices starting downloads.
3. A 100 KB rescue object (2 s at 0.4 Mbps) is fetched over one of the paths in the table below.

| Variant | Rescue path | Closest ns-3 policy |
|---|---|---|
| `l4s` | persistent second connection, DCTCP, placed in the DualPI2 L queue | P2 (L4S bail-out) |
| `ecn` | persistent second connection, Cubic with classic ECN, C queue | Cubic+ECN lane |
| `classic` | persistent second connection, Cubic, Not-ECT | P2-classic |
| `fresh` | new Cubic connection for each rescue; BE keeps running | on-demand second connection |
| `abandon` | reset BE, then refetch on a new connection | P1 / P1-cancel |
| `dctcp_c` | DCTCP left in the C queue (unfair-share control) | DCTCP lane in the classic queue |

## Questions

1. Does the gain depend on missing loss recovery? The ns-3 draft explains the gain this way: a small fetch that loses a segment waits for an RTO, and ECN or L4S avoids the loss. ns-3 has no RACK-TLP, while Linux enables it by default. Every variant therefore runs twice:
   - TLP/RACK on (Linux default);
   - TLP/RACK off (close to ns-3).

   If the classic, new-connection and abandon paths catch up with TLP on, the L4S claim needs to change.
2. How do the paths compare on a real stack? L4S vs. classic ECN vs. a plain second connection vs. abandon.
3. How do the queues behave during the transient? Classic and L-queue delay on DualPI2, compared with ns-3 Fig. 1.
4. What if the transient comes from other devices' traffic? The testbed covers both other devices starting downloads (`onset`) and a capacity drop (`step`).
5. What is the lane worth without an L4S AQM? This uses a FIFO bottleneck.

There is no canary: the lane is warmed once and then left idle, as Linux would leave it. This removes the overhead question from the comparison.

## Requirements (Linux, root)

- Kernel: Linux 6.17 or later (DualPI2 is in mainline from 6.17), and an iproute2 that supports `dualpi2`.
- Tools: the `tcp_dctcp` module, `nft` or `iptables`, `ethtool`, `ss`, and Python 3.8 or later.
- Optional: `matplotlib`, for the figures.

Check with:

```bash
sudo ./preflight.sh
```

## Running

Short test (about 15 minutes):

```bash
sudo QUICK=1 ./run_all.sh
```

Full matrix (about 3.5 hours):

```bash
sudo ./run_all.sh
```

Step by step:

1. Bring up the topology:
   ```bash
   sudo AQM=dualpi2 RATE=50mbit RTT_MS=30 ./topo.sh up
   ```
2. Run the self-test:
   ```bash
   sudo ip netns exec cli python3 tb.py selftest
   ```
3. Run the experiment:
   ```bash
   sudo ip netns exec cli python3 tb.py rescue --event step --out results/dualpi2_step.jsonl
   ```
4. Analyze the results:
   ```bash
   python3 analyze.py results/*.jsonl --out results/figs
   ```
5. Tear down the topology:
   ```bash
   sudo ./topo.sh down
   ```

### Self-test

`selftest` should show:

| Path | ECN negotiated | L-queue packets |
|---|---|---|
| `classic` | no | 0 |
| `ecn` | yes | 0 |
| `l4s` | yes | more than 0 |

If `l4s` shows no L-queue packets, the rule that moves it into the L queue (`meta priority set 20:1`) is not working.

### Defaults

The defaults follow ns-3 cell A2:
- Network: 50 Mbit/s bottleneck and 30 ms base RTT.
- Traffic: 5 Cubic bulk flows as cross traffic, plus the video's busy BE flow.
- Events: `step` drops the rate from 50 to 8 Mbit/s; `onset` adds 5 bulk flows.
- Rescue: issued 0.1 s or 0.5 s after the event, with a 5 s timeout.
- Repetitions: 20 for each combination of variant, TLP setting and delay, in random order (about 70 minutes per AQM and event).
- DualPI2: Linux defaults (15 ms target, 1 ms step threshold, coupling factor 2).

All of these can be changed with flags on `tb.py rescue` (see `--help`).

## Output files

Each run writes:
- `*.jsonl`: one line per rescue, with completion time, success, connect time, retransmissions, and cwnd before and after.
- `*.qmon.jsonl`: DualPI2 statistics every 20 ms (`delay_c`, `delay_l`, marks).
- `*.meta.json`: kernel, sysctls, qdisc configuration and arguments.

The analysis step (`analyze.py`) writes:
- `figs/summary.md`: for each path, the failure rate; the 50th, 90th and 99th percentile completion times; the share of rescues finished within 0.5, 1 and 1.5 s (Wilson 95% CI); and the mean retransmissions.
- `figs/rescue_cdf_*.png|pdf` and `figs/queue_transient_*.png|pdf`.

## Reading the results

The outcomes below refer to section 3 of assessment_and_plan.md.

- Outcome A: with TLP on, the L4S lane finishes in time clearly more often than `fresh`, `abandon` and `classic`. The L4S claim stands.
- Outcome B: with TLP on, `l4s` and `ecn` are similar, and both beat `classic`. The claim becomes: a rescue path that avoids loss, with L4S as the deployable form.
- Outcome C: with TLP on, `fresh` or `abandon` match `l4s`. The ns-3 gap came mostly from missing loss recovery, and the paper shifts to "the AQM matters, the lane does not".

## Notes on TLP and RACK

"TLP off" sets `tcp_early_retrans=0`. The script also tries `tcp_recovery=0` (RACK off), but some recent kernels keep RACK on regardless. The effective values are stored with every trial (`sysctl` field) and a note is printed if RACK stayed on. TLP is the part that matters for losses at the tail of a small fetch.

## Limitations

- Congestion control. DCTCP is used, not Prague, as in the ns-3 draft. Mainline DCTCP sends ECT(0), so it is placed in the L queue by skb priority rather than by ECT(1).
- Link model. The link is a wired veth with an HTB shaper. There is no Wi-Fi MAC, so the testbed checks only the transport and the AQM.
- Timing. The application is a single-host Python program. Timing resolution is about 1 ms, which is enough for completion times between 100 ms and 5 s.
- No player loop. Each rescue is one object on an otherwise idle lane. A player prototype is the next step.

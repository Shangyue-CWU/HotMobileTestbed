# Testbed run commands

Copy-paste commands for running the L4S rescue-lane testbed, from the Mac to the Linux box and back.
Background, design and how to read the outcome: `README.md` here, and `../testing_record.md` (local only).

Placeholders: `<linux-box>` is the SSH host of the test machine, `<user>` your login there.

## 0. On the Mac: send the testbed to the Linux box

Option A, private GitHub repo (as planned in testing_record.md):

```bash
cd ~/Documents/RL_ShortVideo/HotMobile/testbed
git init -b main
git add .
git commit -m "L4S rescue-lane testbed"
gh repo create l4s-rescue-testbed --private --source=. --push
```

Later changes: `git add -A && git commit -m "..." && git push`.

Option B, plain copy:

```bash
rsync -av --exclude results --exclude __pycache__ ~/Documents/RL_ShortVideo/HotMobile/testbed/ <user>@<linux-box>:~/l4s-rescue-testbed/
```

## 1. On the Linux box: check the kernel first

```bash
uname -r
```

The testbed needs **6.17 or later** (DualPI2 entered mainline in 6.17).

- 6.17 or later: go to step 2.
- Older: do not swap the kernel on the CUDA training machine, because the NVIDIA driver may not build against it. Use a VM instead, for example an Ubuntu 26.04 LTS guest under KVM or multipass (4 vCPU, 8 GB, 20 GB disk is plenty). Run `uname -r` inside the VM to confirm 6.17+. All three namespaces live inside the VM, so nothing else changes.

### 1b. VM with `vm.sh` (CUDA box: kernel 6.8, iproute2 6.1, no dualpi2)

Ubuntu 26.04 has kernel 7.0 and iproute2 6.19, so it has dualpi2 in both and needs no source build. From the repo on the host:

```bash
sudo snap install multipass     # once
./vm.sh up                      # 26.04 VM, 4 vCPU / 8 GB / 20 GB; installs packages, copies the code, runs preflight
./vm.sh shell                   # code is in ~/l4s-rescue-testbed; continue with step 3 there
```

- After changing code on the host: `./vm.sh push`.
- To copy results back to `./results/` on the host: `./vm.sh pull`. This replaces the rsync in step 7.
- To remove the VM: `./vm.sh delete`.

Step 2 is done by `vm.sh up`. If its preflight fails on a missing module, run `sudo apt install linux-modules-extra-$(uname -r)` inside the VM.

## 2. On the Linux box: install and preflight

```bash
sudo apt update
sudo apt install -y git iproute2 nftables ethtool python3 python3-matplotlib tmux
```

Get the code (Option A):

```bash
git clone git@github.com:<github-user>/l4s-rescue-testbed.git ~/l4s-rescue-testbed
cd ~/l4s-rescue-testbed
chmod +x *.sh tb.py analyze.py
sudo ./preflight.sh
```

All lines should be `[ok]` and the last line `preflight passed`. Warnings about matplotlib are fine; the analysis can be done on the Mac.

If preflight says `cannot create a dualpi2 qdisc` on a 6.17+ kernel:

```bash
sudo modprobe sch_dualpi2
tc -V
```

If the module loads but `tc` still refuses, the iproute2 package is too old for `dualpi2`. Build it from source:

```bash
sudo apt install -y build-essential bison flex pkg-config libmnl-dev libelf-dev
git clone https://git.kernel.org/pub/scm/network/iproute2/iproute2.git ~/iproute2
cd ~/iproute2 && ./configure && make -j && sudo make install
cd ~/l4s-rescue-testbed && sudo ./preflight.sh
```

## 3. On the Linux box: self-test alone (about 1 minute)

Do this before any timed run. It checks ECN negotiation and L-queue steering.

```bash
cd ~/l4s-rescue-testbed
sudo env AQM=dualpi2 RATE=50mbit RTT_MS=30 ./topo.sh up
sudo ./topo.sh show
sudo ip netns exec cli python3 tb.py selftest
sudo ./topo.sh down
```

Expected:

| Path | ecn_negotiated | L-queue packets |
|---|---|---|
| classic | False | 0 or None |
| ecn | True | 0 or None |
| l4s | True | more than 0 |

If `l4s` shows no L-queue packets, the `meta priority set 20:1` rule is not working. Stop here and send me the full selftest output plus `sudo ./topo.sh show`.

## 4. On the Linux box: quick run (about 10 to 15 minutes)

DualPI2, capacity step, 3 repetitions per cell. Run inside tmux so an SSH drop does not kill it.

```bash
tmux new -s l4s
cd ~/l4s-rescue-testbed
sudo env QUICK=1 OUT=results/quick ./run_all.sh 2>&1 | tee results_quick.log
```

(Detach with `Ctrl-b d`, reattach with `tmux attach -t l4s`.)

Checks after it finishes:

```bash
cat results/quick/selftest_dualpi2.txt
cat results/quick/figs/summary.md
grep -c FAIL results_quick.log
grep "note: kernel kept" results_quick.log
```

Confirm that TLP really switched off in the `tlp=False` trials:

```bash
python3 -c "import json,sys,collections; c=collections.Counter((r['tlp'], json.dumps(r['sysctl'], sort_keys=True)) for r in map(json.loads, open(sys.argv[1]))); [print(k, v) for k, v in sorted(c.items())]" results/quick/dualpi2_step.jsonl
```

Expected: `tlp=True` rows show `tcp_early_retrans: 3`, `tlp=False` rows show `tcp_early_retrans: 0`. `tcp_recovery` may stay at 1 on recent kernels (RACK kept on); that is recorded, not an error.

Warning signs that mean "stop and look before the full run":
- almost every rescue is `FAIL`, or every path has the same completion time;
- `summary.md` is empty, or one path is missing;
- the run stopped with `could not set tcp_early_retrans`.

## 5. On the Linux box: full run (about 3 to 3.5 hours)

Only after step 4 looks right. Covers DualPI2 step, DualPI2 onset, and FIFO step (l4s, classic, abandon).

```bash
tmux new -s l4s-full
cd ~/l4s-rescue-testbed
sudo env OUT=results/full_$(date +%m%d) ./run_all.sh 2>&1 | tee results_full.log
```

Then give the results back to your user account so they can be copied:

```bash
sudo chown -R $USER:$USER results results_*.log
```

## 6. If a run is interrupted

```bash
sudo pkill -f "tb.py serve"
sudo pkill -f "tb.py qmon"
sudo ./topo.sh down
```

`tb.py` appends to its `.jsonl`, so restart with a **new** `OUT` directory rather than reusing the old one, or the two runs mix.

## 7. On the Mac: bring the results back

```bash
rsync -av <user>@<linux-box>:~/l4s-rescue-testbed/results/ ~/Documents/RL_ShortVideo/HotMobile/testbed/results/
rsync -av <user>@<linux-box>:~/l4s-rescue-testbed/results_*.log ~/Documents/RL_ShortVideo/HotMobile/testbed/results/
```

The `results/` folder is git-ignored, so it never goes into the repo.

## 8. On the Mac: analysis and paper figures

Re-run the summary locally (uses the project Python, which has matplotlib and numpy):

```bash
cd ~/Documents/RL_ShortVideo/HotMobile/testbed
/Users/zhush/anaconda3/bin/python3 analyze.py results/full_MMDD/*.jsonl --out results/full_MMDD/figs
```

Feed the paper figures (Fig. 3 queue transient, Fig. 5 rescue CDF). This replaces the mock CSVs in `figures/data/` with real ones (`mock = 0`); `make_figures.py` does not overwrite real files.

```bash
cd ~/Documents/RL_ShortVideo/HotMobile/figures
/Users/zhush/anaconda3/bin/python3 from_testbed.py ../testbed/results/full_MMDD/dualpi2_step.jsonl
/Users/zhush/anaconda3/bin/python3 make_figures.py
```

For the onset transient instead: `from_testbed.py ../testbed/results/full_MMDD/dualpi2_onset.jsonl --event onset`.

## 9. Log the run

Add one row per run to section 8 of `../testing_record.md`: date, machine and `uname -r`, command, outcome A/B/C, notes.

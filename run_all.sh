#!/usr/bin/env bash
# Full matrix on the Linux box (root). Roughly 70 min per (AQM, event) at the defaults.
#   sudo ./run_all.sh            # full
#   sudo QUICK=1 ./run_all.sh    # ~15 min smoke run: DualPI2 step only, 3 reps
set -euo pipefail
cd "$(dirname "$0")"
OUT=${OUT:-results/$(date +%Y%m%d_%H%M)}
mkdir -p "$OUT"

./preflight.sh

run() {  # run AQM EVENT [extra tb.py args]
    local aqm=$1 event=$2; shift 2
    AQM=$aqm ./topo.sh up
    [ "$aqm" = dualpi2 ] && ip netns exec cli python3 tb.py selftest | tee "$OUT/selftest_$aqm.txt"
    ip netns exec cli python3 tb.py rescue --aqm "$aqm" --event "$event" \
        --out "$OUT/${aqm}_${event}.jsonl" "$@"
    ./topo.sh down
}

if [ "${QUICK:-0}" = 1 ]; then
    run dualpi2 step --reps 3
else
    run dualpi2 step                                    # Q1-Q3: transient from a capacity drop
    run dualpi2 onset                                   # Q4: transient from other devices' downloads
    run fifo step --variants l4s,classic,abandon        # Q5: no L4S AQM at the bottleneck
fi

python3 analyze.py "$OUT"/*.jsonl --out "$OUT/figs" || true
echo "results in $OUT"

#!/usr/bin/env bash
# Check that this Linux box can run the testbed. Run as root.
set -uo pipefail
ok()   { printf '  [ok]   %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; FAILED=1; }
warn() { printf '  [warn] %s\n' "$1"; }
FAILED=0

echo "L4S rescue-lane testbed preflight"
[ "$EUID" -eq 0 ] && ok "running as root" || bad "must run as root (sudo)"

K=$(uname -r)
if [ "$(printf '%s\n6.17\n' "${K%%-*}" | sort -V | head -1)" = "6.17" ]; then
    ok "kernel $K (>= 6.17, DualPI2 in mainline)"
else
    warn "kernel $K < 6.17: DualPI2 may be missing (use a 6.17+ mainline kernel or the L4STeam kernel)"
fi

modprobe dummy 2>/dev/null; modprobe sch_dualpi2 2>/dev/null
ip netns add l4s-pf 2>/dev/null
ip -n l4s-pf link add d0 type dummy && ip -n l4s-pf link set d0 up
if tc -n l4s-pf qdisc add dev d0 root dualpi2 2>/dev/null; then
    ok "tc + kernel support dualpi2"
else
    bad "cannot create a dualpi2 qdisc"
    if ! modinfo sch_dualpi2 >/dev/null 2>&1 && ! grep -qs sch_dualpi2 "/lib/modules/$K/modules.builtin"; then
        warn "  kernel $K has no sch_dualpi2: use a 6.17+ kernel, or run in the VM (./vm.sh up, see RUN_COMMANDS.md step 1)"
    fi
    TC_HELP=$(tc qdisc add dev lo root dualpi2 help 2>&1)   # tc exits non-zero here; pipefail would hide a grep match
    if grep -q 'Unknown qdisc' <<<"$TC_HELP"; then
        warn "  $(tc -V 2>&1 | head -1) does not know dualpi2: need iproute2 >= 6.17"
    fi
fi
ip netns del l4s-pf 2>/dev/null

modprobe tcp_dctcp 2>/dev/null
if sysctl -n net.ipv4.tcp_available_congestion_control | grep -qw dctcp; then
    ok "dctcp available"
else
    bad "dctcp congestion control not available (modprobe tcp_dctcp)"
fi
sysctl -n net.ipv4.tcp_available_congestion_control | grep -qw cubic && ok "cubic available" || bad "cubic missing"

if command -v nft >/dev/null; then ok "nft present"
elif command -v iptables >/dev/null; then ok "iptables present (CLASSIFY target used)"
else bad "need nft or iptables for L-queue steering"; fi

command -v ethtool >/dev/null && ok "ethtool present" || warn "ethtool missing: offloads stay on (less faithful)"
command -v ss >/dev/null && ok "ss present" || warn "ss missing: selftest cannot confirm ECN negotiation"

if python3 -c 'import sys; assert sys.version_info >= (3, 8)' 2>/dev/null; then
    ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
else
    bad "python3 >= 3.8 required"
fi
python3 -c 'import matplotlib' 2>/dev/null && ok "matplotlib (for analyze.py)" || warn "matplotlib missing: analyze.py will print tables only"

echo
[ "$FAILED" -eq 0 ] && echo "preflight passed" || { echo "preflight FAILED"; exit 1; }

#!/usr/bin/env bash
# Three-namespace dumbbell for the L4S rescue-lane cross-check.
#
#   srv (10.0.1.1/.2/.3) --- rtr [bottleneck on r1: HTB rate + AQM leaf] --- cli (10.0.2.1)
#
# Server addresses select the lane:
#   10.0.1.1  best-effort / classic lane (Cubic, Not-ECT)          -> C queue
#   10.0.1.2  classic-ECN lane (Cubic, ECT(0) via route feature)   -> C queue, marked
#   10.0.1.3  L4S lane (DCTCP, steered to the L queue by skb prio)  -> L queue
#
# Usage:  sudo AQM=dualpi2 RATE=50mbit RTT_MS=30 ./topo.sh up
#         sudo ./topo.sh down
#         sudo ./topo.sh show
# AQM = dualpi2 | fifo | fq_codel      (FIFO_MS sizes the FIFO at RATE, default 250)
set -euo pipefail

CMD=${1:-up}
AQM=${AQM:-dualpi2}
RATE=${RATE:-50mbit}
RTT_MS=${RTT_MS:-30}
FIFO_MS=${FIFO_MS:-250}
PI2_HANDLE=20          # dualpi2 handle (hex); L-queue steering uses priority 20:1

ns() { local n=$1; shift; ip netns exec "$n" "$@"; }

down() {
    for n in srv rtr cli; do ip netns del "$n" 2>/dev/null || true; done
}

rate_bps() {   # "50mbit" -> 50000000
    local r=${1,,}
    case $r in
        *gbit) echo $(( ${r%gbit} * 1000000000 )) ;;
        *mbit) echo $(( ${r%mbit} * 1000000 )) ;;
        *kbit) echo $(( ${r%kbit} * 1000 )) ;;
        *) echo "$r" ;;
    esac
}

up() {
    down
    for n in srv rtr cli; do ip netns add "$n"; ns "$n" ip link set lo up; done

    ip link add s0 netns srv type veth peer name r0 netns rtr
    ip link add c0 netns cli type veth peer name r1 netns rtr

    for a in 1 2 3; do ns srv ip addr add 10.0.1.$a/24 dev s0; done
    ns rtr ip addr add 10.0.1.254/24 dev r0
    ns rtr ip addr add 10.0.2.254/24 dev r1
    ns cli ip addr add 10.0.2.1/24 dev c0
    ns srv ip link set s0 up; ns cli ip link set c0 up
    ns rtr ip link set r0 up; ns rtr ip link set r1 up
    ns srv ip route add default via 10.0.1.254
    ns cli ip route add default via 10.0.2.254
    ns rtr sysctl -qw net.ipv4.ip_forward=1

    # Per-packet behaviour at the bottleneck: no GSO/GRO/TSO super-packets.
    for pair in srv:s0 rtr:r0 rtr:r1 cli:c0; do
        ns "${pair%%:*}" ethtool -K "${pair#*:}" gro off gso off tso off >/dev/null 2>&1 || true
    done

    # ECN: the client asks for it only towards .2 and .3 (per-route feature);
    # the server accepts ECN when asked (tcp_ecn=2, the Linux default).
    ns cli ip route add 10.0.1.2/32 via 10.0.2.254 features ecn
    ns cli ip route add 10.0.1.3/32 via 10.0.2.254 features ecn
    for n in srv cli; do ns "$n" sysctl -qw net.ipv4.tcp_ecn=2; done

    # Linux defaults that matter for small fetches, set explicitly for the record.
    for n in srv cli; do
        ns "$n" sysctl -qw net.ipv4.tcp_early_retrans=3   # TLP on (tb.py toggles this in srv)
        ns "$n" sysctl -qw net.ipv4.tcp_recovery=1        # RACK on
        ns "$n" sysctl -qw net.ipv4.tcp_syn_retries=6
    done

    # Base RTT: all propagation delay on the rtr->srv direction (requests and ACKs),
    # so the bottleneck direction (rtr->cli) holds only the AQM queue.
    ns rtr tc qdisc add dev r0 root netem delay "${RTT_MS}ms" limit 100000

    # Bottleneck: HTB shaper (rate changed at run time by tb.py) with the AQM as leaf.
    ns rtr tc qdisc add dev r1 root handle 1: htb default 10
    local burst=$(( $(rate_bps "$RATE") / 8000 )); [ "$burst" -lt 3000 ] && burst=3000   # ~1 ms, as in tb.py
    ns rtr tc class add dev r1 parent 1: classid 1:10 htb rate "$RATE" ceil "$RATE" burst "$burst" cburst "$burst"
    case $AQM in
        dualpi2)
            ns rtr tc qdisc add dev r1 parent 1:10 handle ${PI2_HANDLE}: dualpi2
            # Steer the L4S lane (server .3) into the L queue: skb->priority = 20:1.
            # Mainline DCTCP sends ECT(0); DualPI2 honours priority <handle>:1 as L4S.
            # Only ECN-capable packets are steered: Not-ECT control packets (SYN-ACK, pure ACKs)
            # stay in the C queue, as they would with real ECT(1) classification.
            if command -v nft >/dev/null; then
                ns rtr nft add table ip l4s
                ns rtr nft add chain ip l4s post '{ type filter hook postrouting priority 0; }'
                ns rtr nft add rule ip l4s post oifname "r1" ip saddr 10.0.1.3 ip ecn != not-ect meta priority set ${PI2_HANDLE}:1
            else
                ns rtr iptables -t mangle -A POSTROUTING -o r1 -s 10.0.1.3 -m ecn ! --ecn-ip-ect 0 -j CLASSIFY --set-class ${PI2_HANDLE}:1
            fi
            ;;
        fifo)
            local bytes=$(( $(rate_bps "$RATE") / 8 * FIFO_MS / 1000 ))
            ns rtr tc qdisc add dev r1 parent 1:10 handle ${PI2_HANDLE}: bfifo limit "$bytes"
            ;;
        fq_codel)
            ns rtr tc qdisc add dev r1 parent 1:10 handle ${PI2_HANDLE}: fq_codel
            ;;
        *) echo "unknown AQM=$AQM" >&2; exit 1 ;;
    esac
    echo "topology up: AQM=$AQM RATE=$RATE RTT=${RTT_MS}ms"
}

show() {
    ns rtr nft list ruleset 2>/dev/null || ns rtr iptables -t mangle -S POSTROUTING
    ns rtr tc -s qdisc show dev r1
    ns rtr tc class show dev r1
    ns rtr tc qdisc show dev r0
}

case $CMD in
    up) up ;;
    down) down ;;
    show) show ;;
    *) echo "usage: $0 up|down|show" >&2; exit 1 ;;
esac

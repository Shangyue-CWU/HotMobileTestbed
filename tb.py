#!/usr/bin/env python3
"""Real-Linux-stack cross-check of the L4S rescue-lane mechanism.

Runs inside the namespaces created by topo.sh (see README.md):
  serve     chunk server                                   (srv namespace)
  qmon      bottleneck queue monitor                       (rtr namespace)
  selftest  check ECN negotiation and L-queue steering     (cli namespace)
  rescue    transient + rescue-fetch experiment            (cli namespace)

`rescue` and `selftest` start `serve` and `qmon` themselves via `ip netns exec`.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.abspath(__file__)
DEV = "r1"                                # bottleneck interface in rtr

REQ = struct.Struct("!Q")                 # request: number of bytes wanted
STATS = struct.Struct("!IIII")            # reply to a stats request
STATS_REQ = 0
BULK = (1 << 63) - 1                      # "send forever"

# struct tcp_info (linux/tcp.h): 8 bytes of u8 fields, then u32 fields.
TCPI_LEN = 104
OFF_LOST = 8 + 6 * 4
OFF_RTT = 8 + 15 * 4
OFF_CWND = 8 + 18 * 4
OFF_TOTAL_RETRANS = 8 + 23 * 4

SERVER_PORTS = {5001: "cubic", 5003: "dctcp"}

# lane -> (server address, port, client-side congestion control)
LANES = {
    "be":      ("10.0.1.1", 5001, "cubic"),   # the video's main connection
    "classic": ("10.0.1.1", 5001, "cubic"),   # Not-ECT, C queue
    "ecn":     ("10.0.1.2", 5001, "cubic"),   # classic ECN (ECT(0)), C queue, marked
    "l4s":     ("10.0.1.3", 5003, "dctcp"),   # DCTCP steered into the L queue
    "dctcp_c": ("10.0.1.2", 5003, "dctcp"),   # DCTCP left in the C queue (unfair control)
}

# variant -> (lane used for the rescue, persistent connection?, abandon BE first?)
VARIANTS = {
    "l4s":     ("l4s", True, False),
    "ecn":     ("ecn", True, False),
    "classic": ("classic", True, False),
    "fresh":   ("classic", False, False),     # new classic connection, BE keeps going
    "abandon": ("classic", False, True),      # dash.js-style: reset BE, refetch on a new connection
    "dctcp_c": ("dctcp_c", True, False),
}


# ---------------------------------------------------------------- helpers

def sh(cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def in_ns(ns, *cmd, check=True):
    return sh(["ip", "netns", "exec", ns, *cmd], check=check)


def recv_exact(sock, n, deadline=None):
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        if deadline is not None:
            left = deadline - time.time()
            if left <= 0:
                raise socket.timeout("deadline")
            sock.settimeout(left)
        k = sock.recv_into(view[got:], n - got)
        if k == 0:
            raise ConnectionError("peer closed")
        got += k
    return bytes(buf)


def tcp_stats(sock):
    b = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, TCPI_LEN)
    u = lambda off: struct.unpack_from("I", b, off)[0]
    return u(OFF_CWND), u(OFF_TOTAL_RETRANS), u(OFF_RTT), u(OFF_LOST)


def htb_burst(mbps):
    # About 1 ms of data (at least 2 MTUs), so HTB bursts do not trip the 1 ms L-queue step threshold.
    return str(max(3000, int(mbps * 125)))


def set_rate(mbps):
    rate, burst = f"{mbps}mbit", htb_burst(mbps)
    in_ns("rtr", "tc", "class", "change", "dev", DEV, "parent", "1:", "classid", "1:10",
          "htb", "rate", rate, "ceil", rate, "burst", burst, "cburst", burst)


def set_tlp(on):
    """TLP on = Linux default. Off = no tail-loss probe (closer to ns-3). RACK is also switched off
    where the kernel still allows it; some recent kernels ignore tcp_recovery=0, so the effective
    values are read back and stored with every trial."""
    want = {"net.ipv4.tcp_early_retrans": 3 if on else 0, "net.ipv4.tcp_recovery": 1 if on else 0}
    for k, v in want.items():
        in_ns("srv", "sysctl", "-qw", f"{k}={v}", check=False)
    got = {k: in_ns("srv", "sysctl", "-n", k, check=False).stdout.strip() for k in want}
    if got["net.ipv4.tcp_early_retrans"] != str(want["net.ipv4.tcp_early_retrans"]):
        sys.exit(f"could not set tcp_early_retrans: {got}")
    if got["net.ipv4.tcp_recovery"] != str(want["net.ipv4.tcp_recovery"]):
        print(f"note: kernel kept tcp_recovery={got['net.ipv4.tcp_recovery']} (RACK stays on)", flush=True)
    return got


def norm(q):
    """tc -j spells DualPI2 stats with hyphens (pkts-in-l, delay-c, ...); use underscores throughout."""
    return {k.replace("-", "_"): v for k, v in q.items()} if q else q


def leaf_qdisc():
    out = in_ns("rtr", "tc", "-s", "-j", "qdisc", "show", "dev", DEV).stdout
    return norm(next((q for q in json.loads(out) if q.get("kind") != "htb"), {}))


def spawn(ns, *args, log=os.devnull):
    return subprocess.Popen(["ip", "netns", "exec", ns, sys.executable, HERE, *args],
                            stdout=subprocess.DEVNULL, stderr=open(log, "a"))


def stop(procs):
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def wait_for_server(timeout=10.0):
    """Block until both server ports accept connections (from the cli namespace)."""
    end = time.time() + timeout
    for ip, port in (("10.0.1.1", 5001), ("10.0.1.3", 5003)):
        while True:
            try:
                socket.create_connection((ip, port), timeout=1.0).close()
                break
            except OSError:
                if time.time() > end:
                    sys.exit(f"server not reachable at {ip}:{port}; see the .serve.log file")
                time.sleep(0.2)


# ---------------------------------------------------------------- server

def handle(conn):
    payload = memoryview(b"\0" * 65536)
    try:
        while True:
            (n,) = REQ.unpack(recv_exact(conn, REQ.size))
            if n == STATS_REQ:
                conn.sendall(STATS.pack(*tcp_stats(conn)))
                continue
            if n == BULK:
                while True:
                    conn.sendall(payload)
            while n > 0:
                k = min(n, len(payload))
                conn.sendall(payload[:k])
                n -= k
    except (OSError, ConnectionError):
        pass
    finally:
        conn.close()


def accept_loop(ls):
    while True:
        conn, _ = ls.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def cmd_serve(a):
    for port, cc in SERVER_PORTS.items():
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())  # inherited by accepted sockets
        ls.bind(("0.0.0.0", port))
        ls.listen(256)
        threading.Thread(target=accept_loop, args=(ls,), daemon=True).start()
    threading.Event().wait()


# ---------------------------------------------------------------- queue monitor

def cmd_qmon(a):
    with open(a.out, "a") as f:
        while True:
            t = time.time()
            r = sh(["tc", "-s", "-j", "qdisc", "show", "dev", a.dev], check=False)
            try:
                leaf = norm(next((q for q in json.loads(r.stdout) if q.get("kind") != "htb"), None))
            except (ValueError, StopIteration):
                leaf = None
            f.write(json.dumps({"t": t, "q": leaf}) + "\n")
            f.flush()
            time.sleep(max(0.0, a.interval - (time.time() - t)))


# ---------------------------------------------------------------- client side

class Conn:
    def __init__(self, lane, timeout=None):
        ip, port, cc = LANES[lane]
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(timeout)
        t = time.time()
        s.connect((ip, port))
        self.connect_s = time.time() - t
        self.s = s

    def fetch(self, n, deadline=None):
        self.s.sendall(REQ.pack(n))
        recv_exact(self.s, n, deadline)

    def stats(self, timeout=2.0):
        self.s.settimeout(timeout)
        self.s.sendall(REQ.pack(STATS_REQ))
        return STATS.unpack(recv_exact(self.s, STATS.size))

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


class Bulk(threading.Thread):
    """A long download: cross traffic, or the video's busy best-effort connection."""

    def __init__(self, lane="be"):
        super().__init__(daemon=True)
        self.lane = lane
        self.stop = threading.Event()
        self.bytes = 0
        self.conn = None

    def run(self):
        try:
            self.conn = Conn(self.lane, timeout=1.0)
            self.conn.s.sendall(REQ.pack(BULK))
            buf = bytearray(65536)
            while not self.stop.is_set():
                try:
                    k = self.conn.s.recv_into(buf)
                except socket.timeout:
                    continue
                if k == 0:
                    break
                self.bytes += k
        except OSError:
            pass
        finally:
            if self.conn:
                self.conn.close()

    def halt(self):
        self.stop.set()
        if self.conn:
            try:   # closing with unread data sends a RST, like a player resetting the connection
                self.conn.s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.conn.close()


def trial(a, variant, tlp, delta):
    lane, persistent, abandon = VARIANTS[variant]
    rec = dict(variant=variant, tlp=tlp, delta=delta, event=a.event, size=a.size,
               aqm=a.aqm, high=a.high, low=a.low, bg=a.bg, ok=False)
    set_rate(a.high)
    flows = [Bulk() for _ in range(a.bg)]
    be = Bulk("be")
    extra, rescue, pre = [], None, None
    for f in flows + [be]:
        f.start()
    try:
        if persistent:
            rescue = Conn(lane, timeout=a.timeout)
            rescue.fetch(20_000, time.time() + a.timeout)       # handshake + warm-up, no canary
        time.sleep(a.warmup)
        rec["flows_active"] = sum(1 for f in flows + [be] if f.bytes > 0)   # expect bg + 1
        if persistent:
            pre = rescue.stats()
            rec["cwnd_pre"] = pre[0]
        t0 = time.time()
        rec["t0"] = t0
        if a.event == "step":
            set_rate(a.low)
        else:                                                   # competing-traffic onset
            extra = [Bulk() for _ in range(a.onset)]
            for f in extra:
                f.start()
        time.sleep(delta)
        if abandon:
            be.halt()
        t_req = time.time()
        rec["t_req"] = t_req
        deadline = t_req + a.timeout
        if not persistent:
            rescue = Conn(lane, timeout=a.timeout)
            rec["connect_s"] = rescue.connect_s
        rescue.fetch(a.size, deadline)
        rec["completion_s"] = time.time() - t_req
        rec["ok"] = True
        try:
            post = rescue.stats()
            rec.update(retrans=post[1] - (pre[1] if pre else 0), cwnd_post=post[0],
                       srtt_ms=post[2] / 1000.0, lost=post[3])
        except (OSError, ConnectionError):
            pass
    except (OSError, ConnectionError) as e:
        rec["error"] = type(e).__name__
    finally:
        rec["be_bytes"] = be.bytes
        for f in flows + [be] + extra:
            f.halt()
        if rescue:
            rescue.close()
        set_rate(a.high)
        time.sleep(a.cooldown)
    return rec


def write_meta(a, path):
    meta = dict(args=vars(a), host=platform.node(), kernel=platform.release(), t=time.time())
    for ns in ("srv", "cli"):
        meta[f"sysctl_{ns}"] = in_ns(ns, "sysctl", "net.ipv4.tcp_ecn", "net.ipv4.tcp_early_retrans",
                                     "net.ipv4.tcp_recovery", "net.ipv4.tcp_slow_start_after_idle",
                                     check=False).stdout
    meta["qdisc"] = in_ns("rtr", "tc", "qdisc", "show", "dev", DEV, check=False).stdout
    meta["netem"] = in_ns("rtr", "tc", "qdisc", "show", "dev", "r0", check=False).stdout
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)


def cmd_rescue(a):
    variants = a.variants.split(",")
    tlps = [t == "on" for t in a.tlp.split(",")]
    deltas = [float(d) for d in a.deltas.split(",")]
    for v in variants:
        if v not in VARIANTS:
            sys.exit(f"unknown variant {v}; choose from {','.join(VARIANTS)}")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    base = a.out[:-len(".jsonl")] if a.out.endswith(".jsonl") else a.out
    procs = [spawn("srv", "serve", log=base + ".serve.log"),
             spawn("rtr", "qmon", "--dev", DEV, "--out", os.path.abspath(base + ".qmon.jsonl"),
                   log=base + ".qmon.log")]
    wait_for_server()
    write_meta(a, base + ".meta.json")

    matrix = [(v, t, d) for v in variants for t in tlps for d in deltas for _ in range(a.reps)]
    random.Random(a.seed).shuffle(matrix)                       # interleave to avoid drift
    tlp_now, tlp_eff, start = None, {}, time.time()
    try:
        with open(a.out, "a") as f:
            for i, (v, t, d) in enumerate(matrix):
                if t != tlp_now:
                    tlp_eff = set_tlp(t)
                    tlp_now = t
                rec = trial(a, v, t, d)
                rec["i"] = i
                rec["sysctl"] = tlp_eff
                f.write(json.dumps(rec) + "\n")
                f.flush()
                done = i + 1
                eta = (time.time() - start) / done * (len(matrix) - done)
                c = f"{rec['completion_s']:.3f}s" if rec["ok"] else f"FAIL({rec.get('error')})"
                warn = "" if rec.get("flows_active") == a.bg + 1 else f"  [only {rec.get('flows_active')} long flows]"
                print(f"[{done}/{len(matrix)}] {v:8s} tlp={'on ' if t else 'off'} d={d:.2f} "
                      f"-> {c}   eta {eta / 60:.0f} min{warn}", flush=True)
    finally:
        set_tlp(True)
        stop(procs)


def cmd_selftest(a):
    procs = [spawn("srv", "serve")]
    wait_for_server()
    set_rate(a.high)
    try:
        print(f"leaf qdisc: {leaf_qdisc().get('kind')}")
        for lane in ("classic", "ecn", "l4s"):
            before = leaf_qdisc()
            c = Conn(lane, timeout=5)
            c.fetch(3_000_000, time.time() + 20)
            ip = LANES[lane][0]
            r = sh(["ss", "-tniH", "dst", ip], check=False)
            ss = " ".join((r.stdout + r.stderr).split())
            toks = ss.split()
            ecn = any(t in ("ecn", "ecnseen") or t.startswith("accecn") for t in toks)
            c.close()
            after = leaf_qdisc()
            diffs = {k: after[k] - before.get(k, 0) for k in after
                     if isinstance(after[k], (int, float)) and after[k] != before.get(k, 0)}
            l_pkts = diffs.get("pkts_in_l", 0)
            marked = diffs.get("ecn_mark", 0)          # step_mark is a subset of ecn_mark
            print(f"  {lane:8s} ecn_negotiated(ss)={ecn!s:5s} L-queue packets={l_pkts}  "
                  f"CE marks={marked} drops={diffs.get('drops', 0)}  changed stats: {json.dumps(diffs)}")
            print(f"           ss: {ss[:400] or '(ss printed nothing)'}")
        print("expected: classic -> no ECN, no L packets; ecn -> ECN, no L packets; "
              "l4s -> ECN and L packets > 0")
    finally:
        stop(procs)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    q = sub.add_parser("qmon")
    q.add_argument("--dev", default=DEV)
    q.add_argument("--out", required=True)
    q.add_argument("--interval", type=float, default=0.02)
    s = sub.add_parser("selftest")
    s.add_argument("--high", type=int, default=50)
    r = sub.add_parser("rescue")
    r.add_argument("--out", required=True, help="results .jsonl (appended)")
    r.add_argument("--aqm", default=os.environ.get("AQM", "dualpi2"), help="label only; topo.sh sets the AQM")
    r.add_argument("--event", choices=("step", "onset"), default="step")
    r.add_argument("--high", type=int, default=50, help="Mbps before the event")
    r.add_argument("--low", type=int, default=8, help="Mbps after a step event")
    r.add_argument("--onset", type=int, default=5, help="new bulk flows for an onset event")
    r.add_argument("--bg", type=int, default=5, help="classic bulk cross-traffic flows")
    r.add_argument("--size", type=int, default=100_000, help="rescue object bytes (2 s at 0.4 Mbps)")
    r.add_argument("--variants", default="l4s,ecn,classic,fresh,abandon")
    r.add_argument("--tlp", default="on,off")
    r.add_argument("--deltas", default="0.1,0.5", help="rescue issued this many s after the event")
    r.add_argument("--reps", type=int, default=20)
    r.add_argument("--warmup", type=float, default=6.0)
    r.add_argument("--timeout", type=float, default=5.0)
    r.add_argument("--cooldown", type=float, default=2.0)
    r.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    {"serve": cmd_serve, "qmon": cmd_qmon, "selftest": cmd_selftest, "rescue": cmd_rescue}[a.cmd](a)


if __name__ == "__main__":
    main()

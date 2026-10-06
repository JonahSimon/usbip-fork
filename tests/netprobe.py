#!/usr/bin/env python3
"""Probe a ufk demo device through its hidraw node. Clock-independent: delivered rate, gaps, inter-arrival, and a
host-clock round trip (output report written, echoed back as an input report by the server).
usage: netprobe.py /dev/hidrawN [seconds] [echo_hz] [label]"""
import os, select, struct, sys, threading, time

path, secs = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 20
echo_hz = float(sys.argv[3]) if len(sys.argv) > 3 else 125
label = sys.argv[4] if len(sys.argv) > 4 else "probe"
fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
sent = {}
stop = threading.Event()


def echoer():
    t0, n = time.perf_counter(), 0
    while not stop.is_set():
        n += 1
        d = t0 + n / echo_hz - time.perf_counter()
        if d > 0:
            time.sleep(d)
        t = time.perf_counter_ns()
        sent[t] = 1
        try:
            os.write(fd, b"\0" + struct.pack("<Q", t))
        except OSError:
            pass


if echo_hz > 0:
    threading.Thread(target=echoer, daemon=True).start()
stream, echoes, arr = [], [], []
end = time.monotonic() + secs
while time.monotonic() < end:
    if not select.select([fd], [], [], 0.2)[0]:
        continue
    while True:
        try:
            b = os.read(fd, 64)
        except BlockingIOError:
            break
        now = time.perf_counter_ns()
        if len(b) < 16:
            continue
        kind, seq, payload = b[0], struct.unpack("<I", b[4:8])[0], struct.unpack("<Q", b[8:16])[0]
        if kind == 1:
            stream.append(seq)
            arr.append(now)
        elif kind == 2:
            echoes.append((now - payload) / 1e6)
stop.set()
q = lambda v, p: sorted(v)[min(len(v) - 1, int(len(v) * p))] if v else float("nan")
got = len(stream)
produced = (stream[-1] - stream[0] + 1) if got > 1 else 0
gaps = [(b - a) / 1e6 for a, b in zip(arr, arr[1:])]
runs = [b - a - 1 for a, b in zip(stream, stream[1:]) if b - a > 1]
out = dict(got=got, produced=produced, coalesced=produced - got, rate=got / secs,
           gap_p50=q(gaps, .5), gap_p99=q(gaps, .99), gap_max=max(gaps) if gaps else float("nan"),
           worst_run=max(runs) if runs else 0,
           echo_n=len(echoes), echo_sent=len(sent), echo_p50=q(echoes, .5), echo_p95=q(echoes, .95),
           echo_p99=q(echoes, .99), echo_max=max(echoes) if echoes else float("nan"))
print(f"[{label}] stream got={got}/{produced} (coalesced {produced-got}) rate={out['rate']:.1f}/s gap ms p50={out['gap_p50']:.2f} p99={out['gap_p99']:.2f} max={out['gap_max']:.2f} worst_missing_run={out['worst_run']}")
print(f"[{label}] echo   got={len(echoes)}/{len(sent)} rtt ms p50={out['echo_p50']:.2f} p95={out['echo_p95']:.2f} p99={out['echo_p99']:.2f} max={out['echo_max']:.2f}")
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import lib
    lib.record("netprobe_" + label, **out)
except Exception:
    pass

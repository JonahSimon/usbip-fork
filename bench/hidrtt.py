#!/usr/bin/env python3
"""Time HID GET_FEATURE round trips on a hidraw node. usage: hidrtt.py /dev/hidrawN [count]"""
import fcntl, os, statistics, sys, time

def ioctl_get_feature(n): return 0xC0000000 | (n << 16) | (0x48 << 8) | 0x07

dev, count = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2000
fd = os.open(dev, os.O_RDWR)
size = 64
rid = None
for r in (0x50, 0x51, 0x52, 0x53, 0x01, 0x02, 0x00):
    try:
        buf = bytearray(size); buf[0] = r
        fcntl.ioctl(fd, ioctl_get_feature(size), buf)
        rid = r; break
    except OSError:
        continue
if rid is None:
    sys.exit("no feature report answered")
ts = []
for _ in range(count):
    buf = bytearray(size); buf[0] = rid
    t = time.perf_counter()
    fcntl.ioctl(fd, ioctl_get_feature(size), buf)
    ts.append((time.perf_counter() - t) * 1000)
ts.sort()
q = lambda p: ts[min(len(ts) - 1, int(len(ts) * p))]
print(f"report 0x{rid:02x} n={count} min={ts[0]:.3f} p50={q(.5):.3f} p95={q(.95):.3f} p99={q(.99):.3f} max={ts[-1]:.3f} ms mean={statistics.mean(ts):.3f}")

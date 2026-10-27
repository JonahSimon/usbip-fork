"""usage: soakprobe.py OUTDIR SECS /dev/hidrawN... ; logs every stream-report gap >40 ms with epoch time."""
import os, select, struct, sys, time
out, secs, paths = sys.argv[1], float(sys.argv[2]), sys.argv[3:]
fds = {os.open(p, os.O_RDONLY | os.O_NONBLOCK): i for i, p in enumerate(paths)}
last = {i: None for i in fds.values()}; seq = {i: None for i in fds.values()}
cnt = {i: 0 for i in fds.values()}
ev = open(f"{out}/gaps.log", "w", buffering=1); mn = open(f"{out}/minutes.log", "w", buffering=1)
end = time.monotonic() + secs; nextmin = time.monotonic() + 60
while time.monotonic() < end:
    for fd in select.select(list(fds), [], [], 0.2)[0]:
        i = fds[fd]
        while True:
            try: b = os.read(fd, 64)
            except BlockingIOError: break
            now = time.monotonic(); wall = time.time()
            if len(b) < 16 or b[0] != 1: continue
            s = struct.unpack("<I", b[4:8])[0]; cnt[i] += 1
            if last[i] is not None and (now - last[i]) * 1000 > 40:
                ev.write(f"{wall:.3f} pad{i} gap_ms={(now-last[i])*1000:.1f} skipped_seq={s-seq[i]-1}\n")
            last[i], seq[i] = now, s
    if time.monotonic() > nextmin:
        nextmin += 60; mn.write(f"{time.time():.0f} " + " ".join(f"{cnt[i]}" for i in sorted(cnt)) + "\n"); cnt = {i: 0 for i in cnt}
ev.write(f"{time.time():.3f} END\n")

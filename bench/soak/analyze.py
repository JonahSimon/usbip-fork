"""usage: analyze.py RUNDIR [min_gap_ms=100]. Clusters pad stalls and shows what ping, tcpdump, Beelink telemetry and WLAN events saw."""
import re, struct, sys, datetime, collections
d = sys.argv[1]; thr = float(sys.argv[2]) if len(sys.argv) > 2 else 100
off = float(re.search(r"beelink_minus_pc_s=(-?[\d.]+)", open(f"{d}/clock_offset.txt").read()).group(1))
ph = dict(l.strip().split("=", 1)[0:2] for l in open(f"{d}/phases.txt") if "=" in l)
start, load_start, end = float(ph["start"]), float(ph["load_start"].split()[0]), float(ph["end"])
hm = lambda t: datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")
ev = []
for l in open(f"{d}/gaps.log"):
    m = re.match(r"([\d.]+) pad(\d) gap_ms=([\d.]+) skipped_seq=(-?\d+)", l)
    if m: ev.append((float(m[1]), int(m[2]), float(m[3]), int(m[4])))
big = [e for e in ev if e[2] >= thr]
print(f"run {end-start:.0f}s, clock offset {off:+.3f}s; gaps>=40ms: {len(ev)}, >={thr:.0f}ms: {len(big)}")
for name, a, b in (("idle", start, load_start), ("load", load_start, end)):
    g = [e for e in ev if a <= e[0] < b]; print(f"  {name}: gaps>=40ms {len(g)}, >=100ms {sum(e[2]>=100 for e in g)}, >=500ms {sum(e[2]>=500 for e in g)}, worst {max([e[2] for e in g], default=0):.0f} ms")
cl = []
for e in sorted(big):
    if cl and e[0] - cl[-1][-1][0] < 2.0: cl[-1].append(e)
    else: cl.append([e])
ping = {}
for l in open(f"{d}/ping.log"):
    m = re.match(r"\[([\d.]+)\] .*icmp_seq=(\d+) .*time=([\d.]+) ms", l)
    if m: ping[int(m[2])] = (float(m[1]), float(m[3]))
pt = [(t, rtt, s) for s, (t, rtt) in ping.items()]
tele = []
for l in open(f"{d}/beelink_tele_ping.log"):
    p = l.split()
    if len(p) >= 3 and p[0] == "P": tele.append((int(p[1]) / 1000 - off, " ".join(p[2:])))
link = []
for l in open(f"{d}/beelink_tele_link.log"):
    p = l.split()
    if len(p) >= 4 and p[0] == "L": link.append((int(p[1]) / 1000 - off, " ".join(p[2:])))
wl = []
for l in open(f"{d}/beelink_wlan_events.txt"):
    m = re.match(r"(\S+) id=(\d+) (.*)", l.strip())
    if m:
        try: wl.append((datetime.datetime.fromisoformat(m[1]).timestamp(), m[2], m[3]))
        except ValueError: pass
# pcap: Beelink->PC data packets (src port 3241)
pk = []; retr = []; seen = set()
try:
    f = open(f"{d}/cap.pcap", "rb").read(); o = 24
    while o + 16 <= len(f):
        ts, tu, cl_, _ = struct.unpack("<IIII", f[o:o+16]); o += 16; pkt = f[o:o+cl_]; o += cl_
        if len(pkt) < 54 or pkt[12:14] != b"\x08\x00": continue
        ihl = (pkt[14] & 15) * 4; sp, dp, sq = struct.unpack(">HHI", pkt[14+ihl:14+ihl+8])
        if sp != 3241: continue
        t = ts + tu / 1e6; pk.append(t)
        tl = struct.unpack(">H", pkt[16:18])[0] - ihl - ((pkt[14+ihl+12] >> 4) * 4)
        if tl > 0:
            k = (dp, sq)
            if k in seen: retr.append(t)
            seen.add(k)
except FileNotFoundError: pass
print(f"pcap: {len(pk)} Beelink->PC packets, {len(retr)} retransmits total")
for c in cl:
    t0, t1 = c[0][0] - max(x[2] for x in c) / 1000, c[-1][0]
    pads = {x[1] for x in c}; mx = max(x[2] for x in c)
    print(f"\n== stall ~{hm(t0)} ({'load' if t0 >= load_start else 'idle'}): {len(pads)}/8 pads, longest {mx:.0f} ms, ends {t1:.3f}")
    w0, w1 = t0 - 3, t1 + 3
    slow = [(round(t - t0, 2), r) for t, r, s in pt if w0 <= t <= w1 and r >= 40]
    seqs = sorted(s for t, r, s in pt if w0 <= t <= w1); miss = sum(b - a - 1 for a, b in zip(seqs, seqs[1:]))
    print(f"   PC ping (+/-3 s): {len(slow)} replies >=40 ms {slow[:6]}, missing replies {miss}")
    print(f"   Beelink->PC ping: {[(round(t-t0,2), s) for t, s in tele if w0 <= t <= w1][:6]}")
    print(f"   link speed: {[s for t, s in link if w0 - 4 <= t <= w1 + 4][:4]}")
    pw = [x for x in pk if w0 <= x <= w1]; gp = max([b - a for a, b in zip(pw, pw[1:])], default=0)
    print(f"   tcpdump: Beelink->PC packets {len(pw)}, longest silence {gp*1000:.0f} ms, retransmits in window {sum(w0 <= r <= w1 for r in retr)}")
    print(f"   WLAN events (+/-15 s): {[(round(t-t0,1), i, m[:40]) for t, i, m in wl if t0-15 <= t <= t1+15][:5]}")
mins = [l.split() for l in open(f"{d}/minutes.log")]
print("\nreports per pad per minute, min across pads:", [min(map(int, m[1:])) for m in mins])

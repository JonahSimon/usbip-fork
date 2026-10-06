"""Latest result per test and server from results/results.jsonl, so stock and a fork can be compared."""
import json, os, sys
f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "results.jsonl")
if not os.path.exists(f):
    sys.exit("no results yet")
last = {}
for line in open(f):
    r = json.loads(line)
    last[(r["test"], r["server"])] = r
print(f"{'test':38} {'server':10} {'n':>5} {'lost':>5} {'p50 ms':>8} {'p99 ms':>8} {'max ms':>8}")
for (t, s), r in sorted(last.items()):
    g = lambda k: f"{r[k]:8.2f}" if isinstance(r.get(k), (int, float)) else f"{'-':>8}"
    print(f"{t:38} {s[:10]:10} {r.get('sent', r.get('cycles', r.get('attached', ''))):>5} {r.get('lost', ''):>5} {g('p50')} {g('p99')} {g('max')}")

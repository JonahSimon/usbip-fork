"""The real-world part: WiFi-like delay, jitter, loss and bandwidth caps between server and client.
The server runs in its own network namespace joined to this one by a veth pair, and netem shapes only that
pair, so nothing else on the machine is affected. Skipped if namespaces or tc are unavailable.
netem is applied in both directions. A loopback echo is OUT then IN, two network round trips, so expect 4 x delay."""
import os
import subprocess
import unittest

import lib

NS, HOST_IF, NS_IF, HOST_IP, NS_IP = "ufk-srv", "ufk0", "ufk1", "10.99.0.1", "10.99.0.2"


def ip(*a, ns=None):
    return lib.sh(*(["ip", "-n", ns] if ns else ["ip"]), *a)


def shape(spec):
    """spec: list of netem args, or None to clear."""
    for dev, ns in ((HOST_IF, None), (NS_IF, NS)):
        pre = ["ip", "netns", "exec", ns] if ns else []
        if spec is None:
            lib.sh(*pre, "tc", "qdisc", "del", "dev", dev, "root", check=False)
        else:
            lib.sh(*pre, "tc", "qdisc", "replace", "dev", dev, "root", "netem", *spec)


class Netem(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.fw = None
        try:
            lib.sh("ip", "netns", "del", NS, check=False)
            ip("netns", "add", NS)
            ip("link", "add", HOST_IF, "type", "veth", "peer", "name", NS_IF)
            ip("link", "set", NS_IF, "netns", NS)
            ip("addr", "add", f"{HOST_IP}/24", "dev", HOST_IF)
            ip("link", "set", HOST_IF, "up")
            ip("addr", "add", f"{NS_IP}/24", "dev", NS_IF, ns=NS)
            ip("link", "set", NS_IF, "up", ns=NS)
            ip("link", "set", "lo", "up", ns=NS)
            lib.sh("modprobe", "sch_netem")
            # a host firewall (ufw) would drop traffic arriving on the new interface; allow just this one
            if lib.sh("which", "iptables", check=False).returncode == 0:
                cls.fw = ["iptables", "-I", "INPUT", "-i", HOST_IF, "-j", "ACCEPT"]
                lib.sh(*cls.fw)
            rto = os.environ.get("RTO_MIN_MS")  # experiment: lower the TCP minimum retransmit timeout on both ends
            if rto:
                ip("route", "change", "10.99.0.0/24", "dev", HOST_IF, "rto_min", f"{rto}ms")
                ip("route", "change", "10.99.0.0/24", "dev", NS_IF, "rto_min", f"{rto}ms", ns=NS)
            lib.sh("ping", "-c", "1", "-W", "2", NS_IP)
        except (subprocess.CalledProcessError, OSError) as e:
            cls.teardown_net()
            raise unittest.SkipTest(f"cannot build the test network: {getattr(e, 'stderr', e)}")
        cls.srv = lib.Server(netns=NS, host=NS_IP).start()

    @classmethod
    def teardown_net(cls):
        if cls.fw:
            lib.sh(*(["iptables", "-D"] + cls.fw[2:]), check=False)
        lib.sh("ip", "netns", "del", NS, check=False)  # removes the veth pair with it

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "srv", None):
            cls.srv.kill(15)
        lib.cleanup()
        cls.teardown_net()
        super().tearDownClass()

    def tearDown(self):
        shape(None)
        for (_, p) in list(lib.vhci_used_ports()):
            lib.detach(p)
        lib.wait_for(lambda: not lib.vhci_used_ports(), 8)
        lib.cleanup()

    def run_case(self, name, netem, expect_delay_ms=0.0, devices=1, hz=100, secs=5, strict_latency=True):
        shape(netem)
        busids = []
        for i in range(devices):
            g = lib.Gadget(i, "loop").create()
            self.assertEqual(lib.bind(g.busid).returncode, 0)
            busids.append(lib.attach(self.srv, g.busid, timeout=15)[1])
        stats = lib.loop_stream(busids, secs, hz)
        w = lib.merge(stats)
        lib.record(f"netem_{name}", devices=devices, rto_min_ms=os.environ.get("RTO_MIN_MS", "default"), **w)
        for i, s in enumerate(stats):
            self.assertEqual((s["lost"], s["dup"], s["reorder"]), (0, 0, 0), f"device {i}: {lib.fmt(s)}")
        if strict_latency:  # an echo is two USB transactions (OUT then IN), each a full network round trip
            self.assertAlmostEqual(w["p50"], 4 * expect_delay_ms + 0.35, delta=max(1.5, expect_delay_ms * 0.6), msg=lib.fmt(w))
        return w

    def test_clean_link(self):
        self.run_case("clean", None)

    def test_5ms_each_way(self):
        self.run_case("5ms", ["delay", "5ms"], 5)

    def test_20ms_with_jitter(self):
        self.run_case("20ms_jitter", ["delay", "20ms", "5ms"], 20, secs=8, hz=25, strict_latency=False)

    def test_wifi_like_loss_is_hidden_by_tcp_but_costs_latency_spikes(self):
        w = self.run_case("loss1pct", ["delay", "5ms", "2ms", "loss", "1%"], strict_latency=False,
                          secs=int(os.environ.get("LOSS_SECS", "5")), hz=int(os.environ.get("LOSS_HZ", "100")))  # an echo here takes ~20 ms: keep hz <= 40 for long runs
        self.assertLess(w["max"], 3000, "a lost segment should cost a retransmit, not a stall")

    def test_bandwidth_cap_10mbit(self):
        self.run_case("rate10mbit", ["delay", "3ms", "rate", "10mbit"], 3, strict_latency=False)

    def test_8_devices_over_a_20ms_link(self):
        self.run_case("8dev_20ms", ["delay", "20ms"], 20, devices=8, hz=50, strict_latency=False)


if __name__ == "__main__":
    unittest.main()

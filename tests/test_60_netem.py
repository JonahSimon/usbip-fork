"""The real-world part: WiFi-like delay, jitter, loss and bandwidth caps between server and client.
The server runs in its own network namespace (lib.Link), so nothing else on the machine is affected.
Skipped if namespaces or tc are unavailable. netem is applied in both directions. A loopback echo is OUT then
IN, two network round trips, so expect 4 x the one-way delay. Loss experiment: RTO_MIN_MS=<n> LOSS_SECS=40 LOSS_HZ=40."""
import os
import unittest

import lib


class Netem(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.link = lib.Link()
        cls.srv = None
        cls.link.up()
        cls.srv = lib.Server(netns=lib.Link.NS, host=lib.Link.NS_IP).start()

    @classmethod
    def tearDownClass(cls):
        if cls.srv:
            cls.srv.kill(15)
        lib.cleanup()
        cls.link.down()
        super().tearDownClass()

    def tearDown(self):
        self.link.shape(None)
        for (_, p) in list(lib.vhci_used_ports()):
            lib.detach(p)
        lib.wait_for(lambda: not lib.vhci_used_ports(), 8)
        lib.cleanup()

    def run_case(self, name, netem, expect_delay_ms=0.0, devices=1, hz=100, secs=5, strict_latency=True):
        self.link.shape(netem)
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
        if strict_latency:
            self.assertAlmostEqual(w["p50"], 4 * expect_delay_ms + 0.35, delta=max(1.5, expect_delay_ms * 0.6), msg=lib.fmt(w))
        return w

    def test_clean_link(self):
        self.run_case("clean", None)

    def test_5ms_each_way(self):
        self.run_case("5ms", ["delay", "5ms"], 5)

    def test_20ms_with_jitter(self):
        self.run_case("20ms_jitter", ["delay", "20ms", "5ms"], 20, secs=8, hz=25, strict_latency=False)

    def test_wifi_like_loss_is_hidden_by_tcp_but_costs_latency_spikes(self):
        # an echo takes ~20 ms here: keep LOSS_HZ <= 40 or the worker falls behind
        w = self.run_case("loss1pct", ["delay", "5ms", "2ms", "loss", "1%"], strict_latency=False,
                          secs=int(os.environ.get("LOSS_SECS", "5")), hz=int(os.environ.get("LOSS_HZ", "100")))
        self.assertLess(w["max"], 3000, "a lost segment should cost a retransmit, not a stall")

    def test_bandwidth_cap_10mbit(self):
        self.run_case("rate10mbit", ["delay", "3ms", "rate", "10mbit"], 3, strict_latency=False)

    def test_8_devices_over_a_20ms_link(self):
        self.run_case("8dev_20ms", ["delay", "20ms"], 20, devices=8, hz=50, strict_latency=False)


if __name__ == "__main__":
    unittest.main()

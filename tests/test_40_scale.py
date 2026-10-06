"""Many devices at once (bulk echo round trips, so latency here is a full round trip). Stock vhci_hcd exposes 8 high-speed ports per controller (CONFIG_USBIP_VHCI_HC_PORTS)."""
import unittest

import lib

P99 = float(__import__("os").environ.get("SCALE_P99_MS", "10"))


class Scale(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.srv = lib.Server().start()
        cls.gadgets = []

    @classmethod
    def tearDownClass(cls):
        cls.srv.kill(15)
        lib.cleanup()
        super().tearDownClass()

    def tearDown(self):
        for (_, p) in list(lib.vhci_used_ports()):
            lib.detach(p)
        lib.wait_for(lambda: not lib.vhci_used_ports(), 8)
        lib.cleanup()

    def bring_up(self, n):
        """n emulated loopback devices (f_hid caps at 4), bound and attached. Returns their imported busids."""
        busids = []
        for i in range(n):
            g = lib.Gadget(i, "loop").create()
            self.assertEqual(lib.bind(g.busid).returncode, 0)
            busids.append(lib.attach(self.srv, g.busid)[1])
        return busids

    def _scale(self, n, hz=100, secs=4):
        stats = lib.loop_stream(self.bring_up(n), secs, hz)
        worst = lib.merge(stats)
        lib.record(f"scale_{n}_devices_{hz}hz_roundtrip", devices=n, **worst)
        for i, s in enumerate(stats):
            self.assertEqual((s["lost"], s["dup"], s["reorder"]), (0, 0, 0), f"device {i}: {lib.fmt(s)}")
        self.assertLess(worst["p99"], P99, f"worst p99 across {n} devices")

    def test_1_device(self):
        self._scale(1)

    def test_4_devices(self):
        self._scale(4)

    def test_8_devices_the_xbox_adapter_party(self):
        self._scale(8)

    def test_8_devices_each_at_1000hz(self):
        self._scale(8, hz=1000, secs=3)

    def test_ninth_device_fails_cleanly_on_stock_port_limit(self):
        """Record the observed cap. Our fork should raise it; stock stops at the vhci port count."""
        attached = 0
        for i in range(10):
            g = lib.Gadget(i, "loop").create()
            lib.bind(g.busid)
            try:
                lib.attach(self.srv, g.busid, timeout=2)
                attached += 1
            except RuntimeError:
                break
        lib.record("vhci_port_cap", attached=attached)
        self.assertGreaterEqual(attached, 8)
        self.assertTrue(self.srv.alive())


if __name__ == "__main__":
    unittest.main()

"""Latency and rate: the same emulated controller measured natively, then through USB/IP over loopback.
Budgets are env-tunable (LAT_P50_MS, LAT_P99_MS: allowed extra latency over native)."""
import os
import unittest

import lib

P50 = float(os.environ.get("LAT_P50_MS", "0.5"))
P99 = float(os.environ.get("LAT_P99_MS", "2.0"))


class Latency(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.g = lib.Gadget(0).create()
        cls.local = lib.hidraw_of(cls.g.busid)
        cls.srv = lib.Server().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.kill(15)
        lib.cleanup()
        super().tearDownClass()

    def over_usbip(self, seconds, hz):
        """Bind, attach, stream, tear down. Returns stats."""
        self.assertEqual(lib.bind(self.g.busid).returncode, 0)
        port = None
        try:
            port, local = lib.attach(self.srv, self.g.busid)
            return lib.stream([(self.g.hidg(), lib.hidraw_of(local))], seconds, hz)[0]
        finally:
            if port is not None:
                lib.detach(port)
            lib.wait_for(lambda: not lib.vhci_used_ports(), 5)
            lib.wait_available(self.g.busid)
            lib.unbind(self.g.busid)
            lib.hidraw_of(self.g.busid)

    def test_overhead_vs_native_at_500hz(self):
        native = lib.stream([(self.g.hidg(), self.local)], 5, 500)[0]
        usbip = self.over_usbip(5, 500)
        lib.record("latency_native_500hz", **native)
        lib.record("latency_usbip_loopback_500hz", **usbip)
        self.assertEqual((native["lost"], usbip["lost"]), (0, 0))
        self.assertLess(usbip["p50"] - native["p50"], P50, f"native {lib.fmt(native)} / usbip {lib.fmt(usbip)}")
        self.assertLess(usbip["p99"] - native["p99"], P99, f"native {lib.fmt(native)} / usbip {lib.fmt(usbip)}")

    def test_rate_sweep_no_loss_up_to_the_endpoint_limit(self):
        """Interrupt IN at bInterval 4 on a high speed bus polls every 1 ms, so 1000 Hz is the ceiling."""
        for hz in (125, 250, 500, 800):
            s = self.over_usbip(3, hz)
            lib.record(f"rate_{hz}hz", **s)
            self.assertEqual((s["lost"], s["dup"], s["reorder"]), (0, 0, 0), f"{hz} Hz: {lib.fmt(s)}")


if __name__ == "__main__":
    unittest.main()

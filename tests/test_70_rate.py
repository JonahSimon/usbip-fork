"""Delivered input-report rate as the link slows down. Stock USB/IP keeps one interrupt URB in flight per endpoint,
and a report only moves when a URB is waiting at the server, so delivery is capped at 1 / RTT per endpoint. Real
controllers report at 125 to 270 Hz, so a few ms of WiFi round trip is enough to start dropping updates.
Server in its own netns (lib.Link); netem delay applies to both directions, so RTT = 2 x one-way."""
import unittest

import lib


class Rate(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.link = lib.Link()
        cls.srv = None
        cls.link.up()
        cls.srv = lib.Server(netns=lib.Link.NS, host=lib.Link.NS_IP).start()
        cls.g = lib.Gadget(0, "probe").create()
        assert lib.bind(cls.g.busid).returncode == 0

    @classmethod
    def tearDownClass(cls):
        if cls.srv:
            cls.srv.kill(15)
        lib.cleanup()
        cls.link.down()
        super().tearDownClass()

    def deliver(self, one_way_ms, hz, secs=3):
        self.link.shape(["delay", f"{one_way_ms}ms"] if one_way_ms else None)
        port = None
        try:
            port, local = lib.attach(self.srv, self.g.busid, timeout=15)
            return lib.stream([(self.g.hidg(), lib.hidraw_of(local))], secs, hz)[0]
        finally:
            self.link.shape(None)
            if port is not None:
                lib.detach(port)
            lib.wait_for(lambda: not lib.vhci_used_ports(), 5)
            lib.wait_available(self.g.busid)

    def test_rate_sweep_follows_the_one_urb_per_round_trip_cap(self):
        for d in (0, 1, 2, 5, 10):
            s = self.deliver(d, 1000)
            lib.record(f"rate_vs_delay_{d}ms_oneway", one_way_ms=d, **s)
            cap = min(1000.0, 1000.0 / (2 * d)) if d else 1000.0
            self.assertGreater(s["rate"], 0.85 * cap, f"one-way {d} ms: {lib.fmt(s)}")  # sanity floor
            self.assertEqual((s["dup"], s["reorder"]), (0, 0), lib.fmt(s))

    @lib.known_stock_bug("one interrupt URB in flight per endpoint caps delivery at 1/RTT; the fork must decouple "
                         "the report stream from the URB round trip")
    def test_a_270hz_controller_keeps_its_rate_over_a_5ms_link(self):
        """The Steam puck sends about 270 reports/s per slot. At 10 ms RTT stock delivers about 98."""
        s = self.deliver(5, 270)
        lib.record("rate_270hz_source_5ms_oneway", **s)
        self.assertGreater(s["rate"], 0.9 * 270, lib.fmt(s))
        self.assertLess(s["p99"], 30, lib.fmt(s))


if __name__ == "__main__":
    unittest.main()

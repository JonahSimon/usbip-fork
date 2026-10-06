"""End to end through the kernel client (vhci_hcd): attach emulated controllers over loopback and use them."""
import os
import select
import struct
import time
import unittest

import lib

EV_KEY, EV_ABS, BTN_SOUTH, ABS_X = 1, 3, 0x130, 0


class Functional(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.probe = lib.Gadget(0, "probe").create()
        cls.pad = lib.Gadget(1, "pad").create()
        cls.local_hidraw = lib.hidraw_of(cls.probe.busid)
        cls.srv = lib.Server().start()
        for g in (cls.probe, cls.pad):
            r = lib.bind(g.busid)
            assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        cls.srv.kill(15)
        lib.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.ports = []
        for g in (self.probe, self.pad):
            self.assertTrue(lib.wait_available(g.busid), f"{g.busid} not available")

    def tearDown(self):
        for p in self.ports:
            lib.detach(p)
        lib.wait_for(lambda: not lib.vhci_used_ports(), 5)

    def imp(self, g):
        port, local = lib.attach(self.srv, g.busid)
        self.ports.append(port)
        return local

    def test_imported_device_keeps_identity(self):
        local = self.imp(self.probe)
        base = f"{lib.SYS_USB}/{local}"
        self.assertEqual((lib.read(base + "/idVendor"), lib.read(base + "/idProduct")), ("1209", "0001"))
        self.assertEqual((lib.read(base + "/manufacturer"), lib.read(base + "/serial")), (lib.MFR, self.probe.serial))
        self.assertEqual(lib.read(base + "/speed"), "480")
        self.assertEqual(lib.read(base + "/bNumInterfaces").strip(), "1")

    def test_hid_report_descriptor_survives_the_trip(self):
        hr = lib.hidraw_of(self.imp(self.probe))
        self.assertEqual(lib.hidraw_report_desc(hr), lib.PROBE_DESC)

    def test_detach_removes_device_and_unbind_restores_local_use(self):
        local = self.imp(self.probe)
        self.assertTrue(lib.hidraw_of(local))
        lib.detach(self.ports.pop())
        self.assertTrue(lib.wait_for(lambda: not os.path.exists(f"{lib.SYS_USB}/{local}"), 5), "imported device lingers")
        self.assertEqual(lib.unbind(self.probe.busid).returncode, 0)
        self.assertTrue(lib.hidraw_of(self.probe.busid), "local driver did not rebind")
        self.assertEqual(lib.bind(self.probe.busid).returncode, 0)

    def test_report_stream_has_no_loss_dup_or_reorder(self):
        hr = lib.hidraw_of(self.imp(self.probe))
        s = lib.stream([(self.probe.hidg(), hr)], 4, 500)[0]
        lib.record("functional_stream_500hz", **s)
        self.assertEqual((s["lost"], s["dup"], s["reorder"]), (0, 0, 0), lib.fmt(s))

    def test_output_report_reaches_device(self):
        hr = lib.hidraw_of(self.imp(self.probe))
        gd = os.open(self.probe.hidg(), os.O_RDONLY | os.O_NONBLOCK)
        fd = os.open(hr, os.O_RDWR)
        try:
            data = bytes(range(0xA0, 0xA8))
            os.write(fd, b"\0" + data)  # report id 0 is stripped by hidraw
            self.assertTrue(select.select([gd], [], [], 2)[0], "device never saw the output report")
            self.assertEqual(os.read(gd, 64)[:8], data)
        finally:
            os.close(fd)
            os.close(gd)

    def test_gamepad_buttons_and_axes_arrive_as_input_events(self):
        ev = lib.event_of(self.imp(self.pad))
        self.assertTrue(ev, "no evdev node: host did not treat it as a gamepad")
        efd = os.open(ev, os.O_RDONLY | os.O_NONBLOCK)
        gd = os.open(self.pad.hidg(), os.O_WRONLY)
        try:
            time.sleep(0.3)
            os.write(gd, struct.pack("<HBBBB", 0x0001, 255, 128, 0, 0))  # button 1 down, X full
            time.sleep(0.2)
            os.write(gd, struct.pack("<HBBBB", 0x0000, 255, 128, 0, 0))  # button 1 up
            seen, end = [], time.monotonic() + 2
            while time.monotonic() < end:
                if select.select([efd], [], [], 0.2)[0]:
                    b = os.read(efd, 24 * 64)
                    for off in range(0, len(b) - 23, 24):
                        _, _, t, code, val = struct.unpack("<qqHHi", b[off:off + 24])
                        seen.append((t, code, val))
            self.assertIn((EV_KEY, BTN_SOUTH, 1), seen)
            self.assertIn((EV_KEY, BTN_SOUTH, 0), seen)
            self.assertIn((EV_ABS, ABS_X, 255), seen)
        finally:
            os.close(efd)
            os.close(gd)

    def test_two_devices_from_one_server_work_together(self):
        a, b = self.imp(self.probe), self.imp(self.pad)
        self.assertEqual(len(lib.vhci_used_ports()), 2)
        hr = lib.hidraw_of(a)
        s = lib.stream([(self.probe.hidg(), hr)], 2, 500)[0]
        self.assertEqual(s["lost"], 0, lib.fmt(s))
        self.assertTrue(lib.event_of(b))


if __name__ == "__main__":
    unittest.main()

"""Multi-interface devices, shaped like the receivers we care about (the shape is real, the radio is not):
  puck   : serial pair + 4 HID slots, 54-byte reports at ~270 Hz, like the Steam Controller puck (28de:1304)
  dongle : two vendor-class bulk interfaces, a rough stand-in for the Xbox Wireless Adapter's USB side (no isochronous)
Real pucks and dongles stay the final check: usbip never sees pairing, radio or firmware."""
import glob
import multiprocessing as mp
import os
import select
import time
import tty
import unittest

import lib

REPORT = 54
HZ = 270


def interface_classes(busid):
    out = []
    for d in sorted(glob.glob(f"{lib.SYS_USB}/{busid}/{busid}:1.*"), key=lambda p: int(lib.read(p + "/bInterfaceNumber"))):
        out.append(tuple(lib.read(f"{d}/bInterface{k}") for k in ("Class", "SubClass", "Protocol")))
    return out


def endpoint_types(busid):
    """Transfer types across every alternate setting, from the raw descriptor table (sysfs only shows the active one)."""
    with open(f"{lib.SYS_USB}/{busid}/descriptors", "rb") as f:
        d = f.read()
    names, out, i = {0: "Control", 1: "Isoc", 2: "Bulk", 3: "Interrupt"}, set(), 0
    while i + 2 <= len(d) and d[i]:
        if d[i + 1] == 5:
            out.add(names[d[i + 3] & 3])
        i += d[i]
    return out


def _tty_pump(path, seconds, writer, conn):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    total, end = 0, time.monotonic() + seconds
    while time.monotonic() < end:
        sel = select.select([] if writer else [fd], [fd] if writer else [], [], 0.2)
        try:
            if writer and sel[1]:
                total += os.write(fd, b"x" * 4096)
            elif not writer and sel[0]:
                total += len(os.read(fd, 65536))
        except (BlockingIOError, OSError):
            pass
    os.close(fd)
    conn.send(total)


class Puck(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.p = lib.Gadget(0, "puck", slots=4).create()
        cls.classes = interface_classes(cls.p.busid)
        cls.srv = lib.Server().start()
        assert lib.bind(cls.p.busid).returncode == 0

    @classmethod
    def tearDownClass(cls):
        cls.srv.kill(15)
        lib.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.assertTrue(lib.wait_available(self.p.busid), "device not available")
        self.ports = []

    def tearDown(self):
        for p in self.ports:
            lib.detach(p)
        lib.wait_for(lambda: not lib.vhci_used_ports(), 5)

    def imp(self):
        port, local = lib.attach(self.srv, self.p.busid)
        self.ports.append(port)
        return local

    def slots(self, local):
        raws = lib.hidraws_of(local, 4)
        self.assertTrue(raws, "fewer than 4 HID interfaces came up")
        return raws

    def test_all_six_interfaces_survive_the_import(self):
        self.assertEqual(len(self.classes), 6)
        self.assertEqual(self.classes[0][0], "02")  # CDC comm
        self.assertEqual([c[0] for c in self.classes[2:]], ["03"] * 4)
        local = self.imp()
        self.slots(local)
        self.assertEqual(interface_classes(local), self.classes)

    def test_four_slots_stream_at_270hz_without_loss(self):
        raws = self.slots(self.imp())
        stats = lib.stream([(self.p.hidg(i), raws[i]) for i in range(4)], 3, HZ, size=REPORT)
        w = lib.merge(stats)
        lib.record("puck_4slots_270hz", **w)
        for i, s in enumerate(stats):
            self.assertEqual((s["lost"], s["dup"], s["reorder"]), (0, 0, 0), f"slot {i}: {lib.fmt(s)}")
        self.assertLess(w["p99"], 5, lib.fmt(w))

    def test_slots_are_isolated_and_in_order(self):
        raws = self.slots(self.imp())
        fds = [os.open(r, os.O_RDONLY | os.O_NONBLOCK) for r in raws]
        try:
            time.sleep(0.3)
            for i in range(4):
                g = os.open(self.p.hidg(i), os.O_WRONLY)
                for n in range(10):
                    os.write(g, bytes([i, n]).ljust(REPORT, b"\0"))
                os.close(g)
                got = [0] * 4
                end = time.monotonic() + 1.0
                while time.monotonic() < end:
                    ready = select.select(fds, [], [], 0.1)[0]
                    for k, fd in enumerate(fds):
                        if fd in ready:
                            while True:
                                try:
                                    b = os.read(fd, 64)
                                except BlockingIOError:
                                    break
                                got[k] += 1
                                self.assertEqual(b[0], i, f"slot {k} received slot {i}'s report")
                self.assertEqual(got, [10 if k == i else 0 for k in range(4)], f"wrote slot {i}")
        finally:
            for fd in fds:
                os.close(fd)

    def test_serial_traffic_does_not_starve_the_hid_slots(self):
        local = self.imp()
        host_tty = lib.wait_for(lambda: (glob.glob(f"{lib.SYS_USB}/{local}/{local}:1.0/tty/ttyACM*") or [None])[0], 8)
        if not host_tty:
            self.skipTest("no ttyACM node on the imported device")
        host_tty = "/dev/" + os.path.basename(host_tty)
        raws = self.slots(local)
        ctx, secs = mp.get_context("fork"), 4
        (r1, c1), (r2, c2) = ctx.Pipe(False), ctx.Pipe(False)
        # the device writes to its serial port, the host reads: bulk IN saturated while the HID slots run
        pw = ctx.Process(target=_tty_pump, args=(self.p.tty(), secs + 2, True, c1))
        pr = ctx.Process(target=_tty_pump, args=(host_tty, secs + 2, False, c2))
        pr.start()
        pw.start()
        time.sleep(0.5)
        stats = lib.stream([(self.p.hidg(i), raws[i]) for i in range(4)], secs, HZ, size=REPORT)
        moved = r2.recv() if r2.poll(secs + 10) else 0
        for q in (pw, pr):
            q.join(5)
            if q.is_alive():
                q.kill()
        w = lib.merge(stats)
        lib.record("puck_slots_with_serial_flood", serial_bytes=moved, **w)
        self.assertGreater(moved, 100_000, "serial channel moved almost nothing")
        self.assertEqual(w["lost"], 0, lib.fmt(w))
        self.assertLess(w["p99"], 20, lib.fmt(w))


class TwoPucks(lib.Base):
    """f_hid has 4 minors machine-wide, so two pucks get 2 slots each. What matters is two composite devices at once."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.pk = [lib.Gadget(i, "puck", slots=2).create() for i in range(2)]
        cls.srv = lib.Server().start()
        for p in cls.pk:
            assert lib.bind(p.busid).returncode == 0

    @classmethod
    def tearDownClass(cls):
        cls.srv.kill(15)
        lib.cleanup()
        super().tearDownClass()

    def setUp(self):
        for p in self.pk:
            self.assertTrue(lib.wait_available(p.busid))
        self.ports = []

    def tearDown(self):
        for p in self.ports:
            lib.detach(p)
        lib.wait_for(lambda: not lib.vhci_used_ports(), 5)

    def imp(self, p):
        port, local = lib.attach(self.srv, p.busid)
        self.ports.append(port)
        return local, lib.hidraws_of(local, 2)

    def test_two_pucks_stream_four_slots_together(self):
        pairs = []
        for p in self.pk:
            _, raws = self.imp(p)
            pairs += [(p.hidg(i), raws[i]) for i in range(2)]
        stats = lib.stream(pairs, 3, HZ, size=REPORT)
        w = lib.merge(stats)
        lib.record("two_pucks_4slots_270hz", **w)
        for i, s in enumerate(stats):
            self.assertEqual((s["lost"], s["dup"], s["reorder"]), (0, 0, 0), f"slot {i}: {lib.fmt(s)}")

    def test_detaching_one_puck_leaves_the_other_streaming(self):
        a, _ = self.imp(self.pk[0])
        b, raws_b = self.imp(self.pk[1])
        lib.detach(self.ports.pop(0))
        self.assertTrue(lib.wait_for(lambda: not os.path.exists(f"{lib.SYS_USB}/{a}"), 5), "detached puck lingers")
        self.assertTrue(os.path.exists(f"{lib.SYS_USB}/{b}"))
        stats = lib.stream([(self.pk[1].hidg(i), raws_b[i]) for i in range(2)], 2, HZ, size=REPORT)
        self.assertEqual(lib.merge(stats)["lost"], 0, lib.fmt(lib.merge(stats)))


class DongleShape(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.d = lib.Gadget(0, "dongle").create()
        cls.classes = interface_classes(cls.d.busid)
        cls.types = endpoint_types(cls.d.busid)
        cls.srv = lib.Server().start()
        assert lib.bind(cls.d.busid).returncode == 0

    @classmethod
    def tearDownClass(cls):
        cls.srv.kill(15)
        lib.cleanup()
        super().tearDownClass()

    def tearDown(self):
        for (_, p) in list(lib.vhci_used_ports()):
            lib.detach(p)
        lib.wait_for(lambda: not lib.vhci_used_ports(), 5)

    def test_vendor_interfaces_and_endpoints_survive_the_import(self):
        self.assertEqual([c[0] for c in self.classes], ["ff", "ff"])
        _, local = lib.attach(self.srv, self.d.busid)
        lib.wait_for(lambda: len(interface_classes(local)) == len(self.classes), 8)  # interfaces appear after configuration
        self.assertEqual(interface_classes(local), self.classes)
        self.assertEqual(endpoint_types(local), self.types)

    def test_bulk_channel_works_beside_the_isochronous_interface(self):
        _, local = lib.attach(self.srv, self.d.busid)
        s = lib.loop_stream([local], 3, 100)[0]
        lib.record("dongle_shape_bulk_100hz", **s)
        self.assertEqual((s["lost"], s["dup"], s["reorder"]), (0, 0, 0), lib.fmt(s))


if __name__ == "__main__":
    unittest.main()

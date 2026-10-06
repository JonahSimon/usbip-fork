"""Wire-protocol conformance: speak raw USB/IP to the server under test. Any server (stock usbipd, our
Rust fork) must pass these. Uses one emulated controller, exported by `usbip bind`."""
import os
import select
import struct
import time
import unittest

import lib
from lib import Raw

ECONNRESET = -104


def import_ok(srv, busid, timeout=8):
    """Import over a fresh raw connection; the device may be busy for a moment after a prior session."""
    end = time.monotonic() + timeout
    while True:
        r = Raw(srv.host, srv.port)
        status, dev = r.import_(busid)
        if status == 0 or time.monotonic() > end:
            return r, status, dev
        r.close()
        time.sleep(0.2)


class Protocol(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()
        cls.g = lib.Gadget(0).create()
        cls.srv = lib.Server().start()
        r = lib.bind(cls.g.busid)
        assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        cls.srv.kill(15)
        lib.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.assertTrue(lib.wait_available(self.g.busid), "device not available for export")
        self.assertTrue(self.srv.alive())

    # -- discovery
    def test_devlist_lists_the_exported_controller(self):
        r = Raw(self.srv.host, self.srv.port)
        devs = {d["busid"]: d for d in r.devlist()}
        r.close()
        d = devs[self.g.busid]
        self.assertEqual((d["vid"], d["pid"]), (lib.VID, lib.PID))
        self.assertEqual(d["speed"], 3, "high speed")
        self.assertEqual(len(d["ifaces"]), d["nif"], "interface list matches the count")
        self.assertEqual(d["ncfg"], 1)

    def test_devlist_busnum_devnum_match_sysfs(self):
        r = Raw(self.srv.host, self.srv.port)
        d = {d["busid"]: d for d in r.devlist()}[self.g.busid]
        r.close()
        base = f"{lib.SYS_USB}/{self.g.busid}"
        self.assertEqual((d["busnum"], d["devnum"]), (int(lib.read(base + "/busnum")), int(lib.read(base + "/devnum"))))

    def test_devlist_only_exports_bound_devices(self):
        r = Raw(self.srv.host, self.srv.port)
        busids = {d["busid"] for d in r.devlist()}
        r.close()
        for b in busids:
            self.assertTrue(lib.read(f"/sys/bus/usb/drivers/usbip-host/{b}/usbip_status") in ("1", "2"), b)

    # -- import
    def test_import_returns_same_descriptor_as_devlist(self):
        r = Raw(self.srv.host, self.srv.port)
        listed = {d["busid"]: d for d in r.devlist()}[self.g.busid]
        r.close()
        r, status, dev = import_ok(self.srv, self.g.busid)
        r.close()
        self.assertEqual(status, 0)
        for k in ("busid", "busnum", "devnum", "speed", "vid", "pid", "bcd", "dclass", "cfgval", "ncfg", "nif"):
            self.assertEqual(dev[k], listed[k], k)

    def test_import_unknown_busid_is_refused(self):
        r = Raw(self.srv.host, self.srv.port)
        status, dev = r.import_("9-9.9")
        r.close()
        self.assertNotEqual(status, 0)
        self.assertIsNone(dev)

    def test_import_busy_device_is_refused(self):
        a, status, _ = import_ok(self.srv, self.g.busid)
        self.assertEqual(status, 0)
        try:
            b = Raw(self.srv.host, self.srv.port)
            status2, _ = b.import_(self.g.busid)
            b.close()
            self.assertNotEqual(status2, 0, "second import of an in-use device must fail")
        finally:
            a.close()

    # -- malformed input must never wedge or kill the server
    def _closed_or_error(self, payload, wait=3.0):
        r = Raw(self.srv.host, self.srv.port)
        r.send(payload)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if select.select([r.s], [], [], 0.2)[0]:
                try:
                    data = r.s.recv(4096)
                except OSError:
                    return True
                if not data:
                    return True
                if len(data) >= 8 and struct.unpack(">I", data[4:8])[0] != 0:
                    return True  # error status reply
        r.close()
        return False

    def test_wrong_protocol_version_is_rejected(self):
        self.assertTrue(self._closed_or_error(struct.pack(">HHI", 0x0100, lib.OP_REQ_DEVLIST, 0)))

    def test_unknown_opcode_is_rejected(self):
        self.assertTrue(self._closed_or_error(struct.pack(">HHI", lib.VER, 0x8abc, 0)))

    def test_truncated_header_then_close_does_not_hurt(self):
        for n in (1, 3, 5, 7):
            r = Raw(self.srv.host, self.srv.port)
            r.send(struct.pack(">HHI", lib.VER, lib.OP_REQ_IMPORT, 0)[:n])
            r.close()
        r = Raw(self.srv.host, self.srv.port)
        r.send(struct.pack(">HHI", lib.VER, lib.OP_REQ_IMPORT, 0) + b"1-")  # busid cut short
        r.close()
        time.sleep(0.3)
        self.assertTrue(self.srv.alive())
        r = Raw(self.srv.host, self.srv.port)
        self.assertTrue(r.devlist())
        r.close()

    def test_import_busid_without_terminator_is_safe(self):
        r = Raw(self.srv.host, self.srv.port)
        r.send(struct.pack(">HHI", lib.VER, lib.OP_REQ_IMPORT, 0) + b"A" * 32)
        try:
            ver, code, status = struct.unpack(">HHI", r.recv(8))
            self.assertNotEqual(status, 0)
        except (EOFError, OSError):
            pass
        r.close()
        self.assertTrue(self.srv.alive())

    # -- URB traffic after import
    def _session(self):
        r, status, dev = import_ok(self.srv, self.g.busid)
        self.assertEqual(status, 0)
        devid = (dev["busnum"] << 16) | dev["devnum"]
        # a bound device is unconfigured; the client must SET_CONFIGURATION before endpoints exist
        seq = r.submit(devid, 0, 0, 0, setup=struct.pack("<BBHHH", 0, 9, 1, 0, 0))
        rep = r.reply()
        self.assertEqual((rep["seq"], rep["status"]), (seq, 0), "SET_CONFIGURATION")
        return r, devid

    def test_control_get_device_descriptor(self):
        r, devid = self._session()
        try:
            seq = r.ctl_in(devid, lib.get_descriptor_setup(1, 0, 18), 18)
            rep = r.reply()
            self.assertEqual((rep["cmd"], rep["seq"], rep["status"], rep["actual"]), (lib.RET_SUBMIT, seq, 0, 18))
            vid, pid = struct.unpack("<HH", rep["data"][8:12])
            self.assertEqual((rep["data"][0], rep["data"][1], vid, pid), (18, 1, lib.VID, lib.PID))
        finally:
            r.close()

    def test_control_get_string_descriptor_roundtrips_manufacturer(self):
        r, devid = self._session()
        try:
            r.ctl_in(devid, lib.get_descriptor_setup(3, 1, 64, value_lang=0x0409), 64)
            rep = r.reply()
            self.assertEqual(rep["status"], 0)
            self.assertEqual(rep["data"][2:rep["data"][0]].decode("utf-16-le"), lib.MFR)
        finally:
            r.close()

    def test_control_get_hid_report_descriptor_is_bit_exact(self):
        r, devid = self._session()
        try:
            n = len(lib.PROBE_DESC)
            r.ctl_in(devid, lib.get_descriptor_setup(0x22, 0, n, iface=True), n)
            rep = r.reply()
            self.assertEqual((rep["status"], rep["data"]), (0, lib.PROBE_DESC))
        finally:
            r.close()

    def test_pipelined_requests_all_answered_once(self):
        r, devid = self._session()
        try:
            seqs = {r.ctl_in(devid, lib.get_descriptor_setup(1, 0, 18), 18) for _ in range(64)}
            got = [r.reply() for _ in range(64)]
            self.assertEqual({g["seq"] for g in got}, seqs)
            self.assertTrue(all(g["status"] == 0 and g["actual"] == 18 for g in got))
        finally:
            r.close()

    def test_interrupt_in_delivers_device_report(self):
        r, devid = self._session()
        fd = os.open(self.g.hidg(), os.O_WRONLY)
        try:
            seq = r.submit(devid, 1, 1, 16, interval=4)
            payload = bytes(range(16))
            time.sleep(0.2)
            os.write(fd, payload)
            rep = r.reply()
            self.assertEqual((rep["cmd"], rep["seq"], rep["status"], rep["data"]), (lib.RET_SUBMIT, seq, 0, payload))
        finally:
            os.close(fd)
            r.close()

    def test_interrupt_out_payload_reaches_device(self):
        """OUT direction with a payload. (f_hid stalls SET_REPORT when it has an OUT endpoint, so use the interrupt OUT endpoint, ep 2.)"""
        r, devid = self._session()
        fd = os.open(self.g.hidg(), os.O_RDONLY | os.O_NONBLOCK)
        try:
            data = b"\x11\x22\x33\x44\x55\x66\x77\x88"
            seq = r.submit(devid, 2, 0, len(data), payload=data, interval=4)
            rep = r.reply()
            self.assertEqual((rep["seq"], rep["status"]), (seq, 0))
            self.assertTrue(select.select([fd], [], [], 2)[0], "device never saw the report")
            self.assertEqual(os.read(fd, 64)[:8], data)
        finally:
            os.close(fd)
            r.close()

    def test_unlink_cancels_pending_urb_without_a_second_reply(self):
        r, devid = self._session()
        try:
            victim = r.submit(devid, 1, 1, 16, interval=4)  # nothing will ever complete it
            time.sleep(0.3)
            unl = r.unlink(devid, victim)
            seen = []
            end = time.monotonic() + 1.5
            r.s.settimeout(0.3)
            while time.monotonic() < end:
                try:
                    seen.append(r.reply())
                except (TimeoutError, OSError):
                    pass
            ret_unlink = [m for m in seen if m["cmd"] == lib.RET_UNLINK]
            self.assertEqual([(m["seq"], m["status"]) for m in ret_unlink], [(unl, ECONNRESET)])
            self.assertEqual([m for m in seen if m["cmd"] == lib.RET_SUBMIT and m["seq"] == victim], [])
        finally:
            r.close()

    @lib.known_stock_bug("stub_enqueue_ret_unlink() never wakes the TX thread, so the reply waits for other traffic")
    def test_unlink_of_unknown_seqnum_is_answered(self):
        r, devid = self._session()
        try:
            unl = r.unlink(devid, 0xDEAD)
            rep = r.reply()
            self.assertEqual((rep["cmd"], rep["seq"]), (lib.RET_UNLINK, unl))
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main()

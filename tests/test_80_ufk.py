"""The Rust prototype (ufk): protocol conformance against its synthetic devices, then the kernel client end to end.
Needs `cargo build --release` in ../ufk (run.sh does it). Parallel attach, unlink wakeup and prompt session drop are
the stock-usbip problems this server and client are meant to fix."""
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import unittest

import lib
from lib import Raw

UFK = os.environ.get("UFK", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ufk", "target", "release", "ufk"))
REPORT_DESC_LEN = 27


def serve(*args):
    port = lib.free_port()
    p = subprocess.Popen([UFK, "serve", "--port", str(port), "--stats", "0", *map(str, args)], stdout=subprocess.DEVNULL)
    ok = lib.wait_for(lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0, 5)
    if not ok:
        p.kill()
        raise RuntimeError("ufk serve never listened")
    return p, port


def session(port, busid="1-1"):
    r = Raw("127.0.0.1", port)
    st, dev = r.import_(busid)
    assert st == 0, f"import {busid} refused"
    return r, (dev["busnum"] << 16) | dev["devnum"]


@unittest.skipUnless(os.path.exists(UFK), "build ../ufk first (cargo build --release)")
class Protocol(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.p, cls.port = serve("--hz", 270, "--devices", 2)
        cls.slow, cls.slow_port = serve("--hz", 0.05)  # one report per 20 s: URBs stay pending

    @classmethod
    def tearDownClass(cls):
        for p in (cls.p, cls.slow):
            p.kill()
        super().tearDownClass()

    def test_devlist(self):
        r = Raw("127.0.0.1", self.port)
        devs = r.devlist()
        r.close()
        self.assertEqual([d["busid"] for d in devs], ["1-1", "1-2"])
        self.assertTrue(all((d["vid"], d["pid"], d["speed"], d["nif"]) == (0x1209, 2, 3, 1) for d in devs))

    def test_descriptors(self):
        r, devid = session(self.port)
        try:
            r.ctl_in(devid, lib.get_descriptor_setup(1, 0, 18), 18)
            d = r.reply()
            self.assertEqual((d["status"], len(d["data"]), struct.unpack("<HH", d["data"][8:12])), (0, 18, (0x1209, 2)))
            r.ctl_in(devid, lib.get_descriptor_setup(2, 0, 255), 255)
            c = r.reply()
            self.assertEqual((c["status"], c["data"][2], len(c["data"])), (0, 41, 41))
            r.ctl_in(devid, lib.get_descriptor_setup(0x22, 0, REPORT_DESC_LEN, iface=True), REPORT_DESC_LEN)
            self.assertEqual(r.reply()["data"], lib.PROBE_DESC)
            r.ctl_in(devid, lib.get_descriptor_setup(3, 1, 64, value_lang=0x0409), 64)
            s = r.reply()
            self.assertEqual(s["data"][2:s["data"][0]].decode("utf-16-le"), "usbip-fork-demo")
            r.ctl_in(devid, lib.get_descriptor_setup(0x2A, 0, 8), 8)  # unknown type: stall, not a hang
            self.assertEqual(r.reply()["status"], -32)
        finally:
            r.close()

    def test_import_busy_and_unknown_are_refused(self):
        a, _ = session(self.port)
        try:
            for busid in ("1-1", "9-9"):  # a refused import ends that connection, so one connection each
                b = Raw("127.0.0.1", self.port)
                self.assertNotEqual(b.import_(busid)[0], 0, busid)
                b.close()
        finally:
            a.close()

    def test_stream_report_arrives_and_echo_round_trips(self):
        r, devid = session(self.port, "1-2")
        try:
            r.submit(devid, 1, 1, 16, interval=4)
            rep = r.reply()
            self.assertEqual((rep["status"], rep["actual"], rep["data"][0]), (0, 16, 1))
            r.submit(devid, 2, 0, 8, payload=b"ABCDEFGH", interval=4)
            r.submit(devid, 1, 1, 16, interval=4)
            got = {}
            for _ in range(2):
                m = r.reply()
                got[m["seq"]] = m
            echo = [m for m in got.values() if m["data"][:1] == b"\x02"]
            self.assertEqual(len(echo), 1)
            self.assertEqual(echo[0]["data"][8:16], b"ABCDEFGH")
        finally:
            r.close()

    def test_unlink_pending_urb_gets_one_prompt_reply(self):
        r, devid = session(self.slow_port)
        try:
            victim = r.submit(devid, 1, 1, 16, interval=4)
            time.sleep(0.2)
            t = time.monotonic()
            unl = r.unlink(devid, victim)
            rep = r.reply()
            self.assertEqual((rep["cmd"], rep["seq"], rep["status"]), (lib.RET_UNLINK, unl, -104))
            self.assertLess(time.monotonic() - t, 0.5)
        finally:
            r.close()

    def test_unlink_of_unknown_seqnum_is_answered_promptly(self):
        """Stock usbipd only answers this when other traffic happens to wake its TX thread."""
        r, devid = session(self.slow_port)
        try:
            t = time.monotonic()
            unl = r.unlink(devid, 0xDEAD)
            rep = r.reply()
            self.assertEqual((rep["cmd"], rep["seq"], rep["status"]), (lib.RET_UNLINK, unl, 0))
            self.assertLess(time.monotonic() - t, 0.5)
        finally:
            r.close()

    def test_handshake_fuzz_never_kills_the_server(self):
        rng = lib.fuzz_rng
        valid = [struct.pack(">HHI", lib.VER, lib.OP_REQ_DEVLIST, 0), struct.pack(">HHI32s", lib.VER, lib.OP_REQ_IMPORT, 0, b"1-1")]
        for _ in range(300):
            data = rng.choice([rng.randbytes(rng.randrange(1, 400)), rng.choice(valid)[:rng.randrange(1, 12)],
                               rng.choice(valid) + rng.randbytes(rng.randrange(1, 100))])
            try:
                s = socket.create_connection(("127.0.0.1", self.port), 1)
                s.sendall(data)
                s.close()
            except OSError:
                pass
        self.assertIsNone(self.p.poll(), "server died")
        r = Raw("127.0.0.1", self.port)
        self.assertEqual(len(r.devlist()), 2)
        r.close()

    def test_oversized_urb_is_rejected_not_allocated(self):
        r, devid = session(self.port, "1-2")
        try:
            r.submit(devid, 2, 0, 0x7FFFFFFF)
            self.assertTrue(lib.wait_for(lambda: self.p.poll() is None, 1))
            r.s.settimeout(2)
            with self.assertRaises((EOFError, OSError)):
                r.recv(1)
        finally:
            r.close()
        r2 = Raw("127.0.0.1", self.port)
        self.assertEqual(len(r2.devlist()), 2)
        r2.close()


@unittest.skipUnless(os.path.exists(UFK), "build ../ufk first (cargo build --release)")
class KernelClient(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()

    def setUp(self):
        self.p, self.port = serve("--hz", 270, "--devices", 4)

    def tearDown(self):
        for (_, port) in list(lib.vhci_used_ports()):
            lib.detach(port)
        lib.wait_for(lambda: not lib.vhci_used_ports(), 5)
        self.p.kill()

    def attach(self, busid="1-1"):
        r = subprocess.run([UFK, "attach", f"127.0.0.1:{self.port}", busid], capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)

    def newest_local(self, before):
        used = lib.wait_for(lambda: [v for k, v in lib.vhci_used_ports().items() if k not in before], 8)
        if not used:
            st = open("/sys/devices/platform/vhci_hcd.0/status").read().splitlines()[:4]
            log = lib.sh("journalctl", "-k", "-n", "6", "--no-pager", "-o", "cat", check=False).stdout.strip()
            self.fail(f"device never appeared. vhci: {st} kernel: {log}")
        return used[0]

    def test_attach_gives_a_working_hid_device(self):
        self.attach()
        hr = lib.hidraw_of(self.newest_local({}))
        self.assertTrue(hr)
        self.assertEqual(lib.hidraw_report_desc(hr), lib.PROBE_DESC)

    def test_stream_rate_and_echo_round_trip_over_loopback(self):
        self.attach()
        hr = lib.hidraw_of(self.newest_local({}))
        p = subprocess.run(["python3", "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "netprobe.py"), hr, "4", "125",
                            "ufk_loopback_270hz"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        line = [l for l in p.stdout.splitlines() if "echo" in l][0]
        rtt_p99 = float(line.split("p99=")[1].split()[0])
        stream = [l for l in p.stdout.splitlines() if "stream" in l][0]
        rate = float(stream.split("rate=")[1].split("/")[0])
        self.assertAlmostEqual(rate, 270, delta=270 * 0.05, msg=stream)
        self.assertLess(rtt_p99, 2.0, line)

    def test_parallel_attach_of_four_devices_works(self):
        """Stock `usbip attach` livelocks one of the clients here; ours retries on the next free port."""
        results = {}

        def go(b):
            r = subprocess.run([UFK, "attach", f"127.0.0.1:{self.port}", b], capture_output=True, text=True, timeout=20)
            results[b] = r.returncode
        ts = [threading.Thread(target=go, args=(f"1-{i}",)) for i in range(1, 5)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(results, {f"1-{i}": 0 for i in range(1, 5)})
        self.assertEqual(len(lib.wait_for(lambda: len(lib.vhci_used_ports()) == 4 and lib.vhci_used_ports(), 8)), 4)

    def test_killing_the_server_drops_the_session_promptly(self):
        self.attach()
        self.newest_local({})
        self.p.kill()
        self.assertTrue(lib.wait_for(lambda: not lib.vhci_used_ports(), 10), "client kept a dead session")

    def test_attach_to_a_busy_device_fails_cleanly(self):
        self.attach()
        self.newest_local({})
        r = subprocess.run([UFK, "attach", f"127.0.0.1:{self.port}", "1-1"], capture_output=True, text=True, timeout=15)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(lib.vhci_used_ports()), 1)


if __name__ == "__main__":
    unittest.main()

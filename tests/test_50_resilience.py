"""Things going wrong: teardown races, crashes, churn, junk on the wire.
EXPECT_SESSION_SURVIVES_SERVER_KILL: stock usbipd hands the socket to the kernel, so killing the daemon
does not drop attached devices (1). A userspace fork may behave differently; set 0 to expect the drop.
FUZZ_URB=1 also fuzzes the kernel's stub after import. Off by default: it can oops a real kernel."""
import os
import random
import select
import socket
import struct
import threading
import time
import unittest

import lib
from lib import Raw

SURVIVES = os.environ.get("EXPECT_SESSION_SURVIVES_SERVER_KILL", "1") == "1"
CYCLES = int(os.environ.get("CYCLES", "100"))


class Resilience(lib.Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lib.cleanup()

    def setUp(self):
        lib.cleanup()
        self.srv = lib.Server().start()
        self.g = lib.Gadget(0).create()
        r = lib.bind(self.g.busid)
        self.assertEqual(r.returncode, 0, f"bind {self.g.busid}: {r.stderr.strip()} (vhci ports in use: {lib.vhci_used_ports()})")

    def tearDown(self):
        self.srv.kill(15)
        lib.cleanup()

    def attach(self, g=None):
        g = g or self.g
        port, local = lib.attach(self.srv, g.busid)
        return port, local

    def test_server_side_unbind_while_attached_frees_the_client_port(self):
        port, local = self.attach()
        self.assertTrue(lib.hidraw_of(local), "device never finished coming up")  # unbind racing attach flakes
        self.assertEqual(lib.unbind(self.g.busid).returncode, 0)
        self.assertTrue(lib.wait_for(lambda: not lib.vhci_used_ports(), 10), "client still thinks it is attached")
        self.assertTrue(lib.wait_for(lambda: not os.path.exists(f"{lib.SYS_USB}/{local}"), 5))

    def test_detach_while_a_reader_is_blocked_does_not_hang(self):
        port, local = self.attach()
        fd = os.open(lib.hidraw_of(local), os.O_RDONLY)
        res = {}

        def rd():
            try:
                res["data"] = os.read(fd, 64)
            except OSError as e:
                res["err"] = e.errno
        t = threading.Thread(target=rd, daemon=True)
        t.start()
        time.sleep(0.3)
        lib.detach(port)
        t.join(5)
        self.assertFalse(t.is_alive(), "reader stuck after detach")
        os.close(fd)

    def test_attaching_the_same_device_twice_is_refused(self):
        self.attach()
        r = lib.sh(*lib.USBIP, "-t", str(self.srv.port), "attach", "-r", self.srv.host, "-b", self.g.busid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(lib.vhci_used_ports()), 1)

    def test_detaching_an_empty_port_changes_nothing(self):
        lib.detach(5)  # stock prints "already detached" and exits 0, so only side effects are checked
        self.assertEqual(lib.vhci_used_ports(), {})
        self.assertTrue(self.srv.alive())

    def test_killing_the_server_process(self):
        port, local = self.attach()
        hr = lib.hidraw_of(local)
        self.srv.kill(9)
        time.sleep(0.5)
        s = lib.stream([(self.g.hidg(), hr)], 1, 250)[0] if SURVIVES else None
        if SURVIVES:
            self.assertEqual(s["lost"], 0, "session should outlive the daemon: " + lib.fmt(s))
        else:
            self.assertTrue(lib.wait_for(lambda: not lib.vhci_used_ports(), 10), "session should have dropped")

    def test_new_sessions_work_after_the_server_restarts(self):
        self.srv.kill(9)
        self.srv = lib.Server().start()
        port, local = self.attach()
        s = lib.stream([(self.g.hidg(), lib.hidraw_of(local))], 1, 250)[0]
        self.assertEqual(s["lost"], 0, lib.fmt(s))

    def test_attach_detach_cycles_leave_nothing_behind(self):
        times = []
        for _ in range(CYCLES):
            t = time.perf_counter()
            port, local = self.attach()
            self.assertTrue(lib.hidraw_of(local))
            lib.detach(port)
            self.assertTrue(lib.wait_for(lambda: not lib.vhci_used_ports(), 5), "port leaked")
            self.assertTrue(lib.wait_available(self.g.busid, 8), "device never became exportable again")
            times.append((time.perf_counter() - t) * 1000)
        times.sort()
        lib.record("cycle_time_ms", cycles=CYCLES, p50=times[len(times) // 2], max=times[-1])
        self.assertTrue(self.srv.alive())

    @lib.known_stock_bug("concurrent `usbip attach` clients race for the same vhci port: one livelocks at 100% CPU "
                         "while the kernel logs 'port N already used' in a loop")
    def test_parallel_attach_detach_of_several_devices(self):
        gs = [self.g] + [lib.Gadget(i, "loop").create() for i in range(1, 5)]
        for g in gs[1:]:
            self.assertEqual(lib.bind(g.busid).returncode, 0)
        errors = []

        def churn(g):
            try:
                for _ in range(15):
                    port, _ = lib.attach(self.srv, g.busid)
                    lib.detach(port)
                    lib.wait_available(g.busid, 8)
            except Exception as e:
                errors.append(f"{g.busid}: {e}")
        ts = [threading.Thread(target=churn, args=(g,)) for g in gs]
        [t.start() for t in ts]
        [t.join(120) for t in ts]
        self.assertEqual(errors, [])
        self.assertTrue(lib.wait_for(lambda: not lib.vhci_used_ports(), 8), "ports leaked under churn")

    def test_handshake_fuzz_never_kills_the_server(self):
        rng, valid = lib.fuzz_rng, [struct.pack(">HHI", lib.VER, lib.OP_REQ_DEVLIST, 0),
                                    struct.pack(">HHI32s", lib.VER, lib.OP_REQ_IMPORT, 0, self.g.busid.encode())]
        for i in range(400):
            kind = rng.randrange(4)
            if kind == 0:
                data = rng.randbytes(rng.randrange(1, 600))
            elif kind == 1:
                data = bytearray(rng.choice(valid))
                for _ in range(rng.randrange(1, 4)):
                    data[rng.randrange(len(data))] = rng.randrange(256)
                data = bytes(data)
            elif kind == 2:
                v = rng.choice(valid)
                data = v[:rng.randrange(1, len(v))]
            else:
                data = rng.choice(valid) + rng.randbytes(rng.randrange(1, 200))
            try:
                s = socket.create_connection((self.srv.host, self.srv.port), 1)
                s.sendall(data)
                if select.select([s], [], [], 0.02)[0]:
                    s.recv(65536)
                s.close()
            except OSError:
                pass
            if i % 50 == 49:
                self.assertTrue(self.srv.alive(), f"server died on iteration {i}")
        time.sleep(0.5)
        self.assertTrue(lib.wait_available(self.g.busid, 8), "fuzz left the device stuck")
        r = Raw(self.srv.host, self.srv.port)
        self.assertTrue(r.devlist())
        r.close()
        # still importable
        port, _ = self.attach()
        lib.detach(port)

    @unittest.skipUnless(os.environ.get("FUZZ_URB") == "1", "FUZZ_URB=1 to fuzz the kernel stub (can oops)")
    def test_urb_fuzz_after_import(self):
        rng = lib.fuzz_rng
        for _ in range(100):
            r = Raw(self.srv.host, self.srv.port)
            st, dev = r.import_(self.g.busid)
            if st == 0:
                devid = (dev["busnum"] << 16) | dev["devnum"]
                for _ in range(rng.randrange(1, 6)):
                    hdr = struct.pack(">IIIII", rng.choice([1, 2, 3, 99]), rng.randrange(1 << 32), devid if rng.random() < .7 else rng.randrange(1 << 32),
                                      rng.randrange(3), rng.randrange(20)) + rng.randbytes(28)
                    try:
                        r.send(hdr + rng.randbytes(rng.randrange(0, 64)))
                    except OSError:
                        break
            r.close()
            lib.wait_available(self.g.busid, 8)
        self.assertTrue(self.srv.alive())


if __name__ == "__main__":
    unittest.main()

"""Shared helpers for the usbip-fork test suite. Run as root (configfs, usbip bind).

Emulated controllers are real USB gadgets on dummy_hcd (a virtual host controller), so they look like
physical USB devices to usbip: they can be bound, exported and imported. Only devices whose manufacturer
string is MFR are ever bound, so the suite can't touch your keyboard, mouse or real controllers.

The server and client under test come from env vars, so the same tests run against the stock tools and
against the fork later: USBIPD_CMD (default "usbipd"), USBIP_CMD (default "usbip").
"""
import fcntl
import ctypes
import glob
import json
import multiprocessing as mp
import os
import random
import re
import select
import socket
import struct
import subprocess
import time

CFG = "/sys/kernel/config/usb_gadget"
SYS_USB = "/sys/bus/usb/devices"
MFR = "usbip-fork-test"
VID, PID = 0x1209, 0x0001  # pid.codes test VID/PID
USBIPD = os.environ.get("USBIPD_CMD", "usbipd").split()
USBIP = os.environ.get("USBIP_CMD", "usbip").split()
RESULTS = os.environ.get("RESULTS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results"))
NUM_GADGETS = 16

# Report descriptors. PROBE: 16-byte vendor input (seq, timestamp) + 8-byte output. PAD: plain gamepad.
PROBE_DESC = bytes([
    0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01,
    0x09, 0x02, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x10, 0x81, 0x02,
    0x09, 0x03, 0x95, 0x08, 0x91, 0x02,
    0xC0])
PAD_DESC = bytes([
    0x05, 0x01, 0x09, 0x05, 0xA1, 0x01,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x10, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x10, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35, 0x15, 0x00, 0x26, 0xFF, 0x00,
    0x75, 0x08, 0x95, 0x04, 0x81, 0x02,
    0xC0])
PUCK_DESC = bytes([  # one vendor-defined 64-byte slot, like each controller interface on the Steam puck
    0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01,
    0x09, 0x02, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x40, 0x81, 0x02,
    0x09, 0x03, 0x95, 0x40, 0x91, 0x02,
    0xC0])
# (descriptor, endpoint packet size). A report that exactly fills the endpoint packet makes f_hid follow it with a
# zero-length packet, and every other interrupt URB then returns empty (halving the delivered rate). Real devices avoid
# this (puck: 54-byte reports on a 64-byte endpoint), so the probe's 16-byte reports ride a 32-byte endpoint.
PROFILES = {"probe": (PROBE_DESC, 32), "pad": (PAD_DESC, 16)}
STOCK = USBIPD == ["usbipd"]


def known_stock_bug(why):
    """Strict test that stock usbipd is known to fail: expected-failure on stock, enforced on a fork."""
    import unittest
    def deco(fn):
        fn.__doc__ = (fn.__doc__ or "") + f" [stock bug: {why}]"
        return unittest.expectedFailure(fn) if STOCK else fn
    return deco


def sh(*cmd, check=True, **kw):
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kw)


def wait_for(fn, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def write(path, val):
    with open(path, "wb" if isinstance(val, bytes) else "w") as f:
        f.write(val)


def read(path):
    with open(path) as f:
        return f.read().strip()


def setup_modules(vhci_controllers=None):
    """Load everything. dummy_hcd must be (re)loaded with enough controllers and high speed."""
    for m in ("libcomposite", "usb_f_hid", "usbip-core", "usbip-host"):
        sh("modprobe", m)
    if os.path.isdir("/sys/module/dummy_hcd"):
        if read("/sys/module/dummy_hcd/parameters/num") != str(NUM_GADGETS) and not glob.glob(CFG + "/*"):
            sh("modprobe", "-r", "dummy_hcd")
    if not os.path.isdir("/sys/module/dummy_hcd"):
        sh("modprobe", "dummy_hcd", f"num={NUM_GADGETS}", "is_high_speed=1")
    if vhci_controllers and not vhci_used_ports():
        sh("modprobe", "-r", "vhci-hcd", check=False)
        sh("modprobe", "vhci-hcd", f"num_controllers={vhci_controllers}")
    else:
        sh("modprobe", "vhci-hcd")


class Gadget:
    """One emulated device on dummy_udc.<idx>.
    probe / pad: one HID interface.  loop: bulk echo (no minor limit).
    puck: serial (2 interfaces) + `slots` HID interfaces of 64 bytes, shaped like the Steam puck. f_hid has 4 minors
          machine-wide, so slots across all live gadgets must total 4.
    dongle: two vendor-class bulk interfaces (echo + source/sink), a rough stand-in for the Xbox dongle's USB side.
          Isochronous endpoints (headset audio on the real one) can't be had from dummy_hcd with this function: untested."""

    def __init__(self, idx, profile="probe", serial=None, slots=4):
        self.idx, self.profile, self.slots = idx, profile, slots
        self.serial = serial or f"ufk{idx}"
        self.g = f"{CFG}/{self.serial}"
        self.busid = None

    def functions(self):
        """[(configfs function name, {attribute: value})] in interface order."""
        if self.profile in PROFILES:
            desc, rlen = PROFILES[self.profile]
            return [("hid.usb0", {"protocol": "0", "subclass": "0", "report_length": str(rlen), "report_desc": desc})]
        if self.profile == "loop":
            return [("Loopback.usb0", {})]
        if self.profile == "puck":
            hid = {"protocol": "0", "subclass": "0", "report_length": "64", "report_desc": PUCK_DESC}
            return [("acm.usb0", {})] + [(f"hid.usb{i}", dict(hid)) for i in range(self.slots)]
        if self.profile == "dongle":
            return [("Loopback.usb0", {}), ("SourceSink.usb0", {})]
        raise ValueError(self.profile)

    def create(self):
        g = self.g
        # a gadget destroyed an instant ago can linger in sysfs under the same serial; wait it out or we latch onto
        # its (soon to vanish) bus id
        if not wait_for(lambda: not self._find_busid(), 8):
            raise RuntimeError(f"previous {self.serial} never left the bus")
        os.makedirs(g)
        write(g + "/idVendor", hex(VID))
        write(g + "/idProduct", hex(PID))
        write(g + "/bcdUSB", "0x0200")
        os.makedirs(g + "/strings/0x409")
        write(g + "/strings/0x409/manufacturer", MFR)
        write(g + "/strings/0x409/product", f"emulated {self.profile}")
        write(g + "/strings/0x409/serialnumber", self.serial)
        os.makedirs(g + "/configs/c.1/strings/0x409")
        write(g + "/configs/c.1/strings/0x409/configuration", "cfg")
        for name, attrs in self.functions():  # link order = interface order
            fn = f"{g}/functions/{name}"
            os.makedirs(fn)
            for k, v in attrs.items():
                write(f"{fn}/{k}", v)
            os.symlink(fn, f"{g}/configs/c.1/{name}")
        write(g + "/UDC", f"dummy_udc.{self.idx}")
        self.busid = wait_for(self._find_busid, 8)
        if not self.busid:
            raise RuntimeError(f"gadget {self.serial} never enumerated")
        return self

    def _find_busid(self):
        for d in glob.glob(SYS_USB + "/[0-9]*-[0-9]*"):
            if ":" in d:
                continue
            try:
                if read(d + "/manufacturer") == MFR and read(d + "/serial") == self.serial:
                    return os.path.basename(d)
            except OSError:
                pass

    def hidg(self, i=0):
        """Device-side node of HID function i: write input reports here, read output reports."""
        major, minor = map(int, read(f"{self.g}/functions/hid.usb{i}/dev").split(":"))
        for p in glob.glob("/dev/hidg*"):
            if os.stat(p).st_rdev == os.makedev(major, minor):
                return p
        raise RuntimeError("no /dev/hidg node")

    def tty(self):
        """Device-side serial node of the acm function."""
        return "/dev/ttyGS" + read(f"{self.g}/functions/acm.usb0/port_num")

    def destroy(self):
        g = self.g
        if not os.path.isdir(g):
            return

        def quietly(fn, *a):
            try:
                fn(*a)
            except OSError:
                pass
        quietly(write, g + "/UDC", "\n")
        for l in glob.glob(g + "/configs/c.1/*"):
            if os.path.islink(l):
                quietly(os.unlink, l)
        quietly(os.rmdir, g + "/configs/c.1/strings/0x409")
        quietly(os.rmdir, g + "/configs/c.1")
        for f in glob.glob(g + "/functions/*"):
            quietly(os.rmdir, f)
        quietly(os.rmdir, g + "/strings/0x409")
        quietly(os.rmdir, g)
        wait_for(lambda: not self._find_busid(), 8)  # removal from the bus is asynchronous

    def __enter__(self):
        return self.create()

    def __exit__(self, *a):
        self.destroy()


def destroy_all_gadgets():
    for g in glob.glob(CFG + "/ufk*"):
        Gadget(0, serial=os.path.basename(g)).destroy()


def hidraw_of(busid):
    """Host-side /dev/hidrawN for a USB device on the local bus (gadget bus or imported device)."""
    def find():
        p = glob.glob(f"{SYS_USB}/{busid}/{busid}:1.0/*/hidraw/hidraw*")
        return "/dev/" + os.path.basename(p[0]) if p else None
    return wait_for(find, 8)


def hidraws_of(busid, count):
    """Host-side hidraw nodes of a multi-interface device, ordered by interface number."""
    def find():
        found = []
        for p in glob.glob(f"{SYS_USB}/{busid}/{busid}:1.*/*/hidraw/hidraw*"):
            found.append((int(re.search(r":1\.(\d+)/", p).group(1)), "/dev/" + os.path.basename(p)))
        return [n for _, n in sorted(found)] if len(found) >= count else None
    return wait_for(find, 8)


def event_of(busid):
    def find():
        p = glob.glob(f"{SYS_USB}/{busid}/{busid}:1.0/*/input/input*/event*")
        return "/dev/input/" + os.path.basename(p[0]) if p else None
    return wait_for(find, 8)


def hidraw_report_desc(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        size = struct.unpack("i", fcntl.ioctl(fd, 0x80044801, b"\0" * 4))[0]  # HIDIOCGRDESCSIZE
        buf = bytearray(struct.pack("I", size) + b"\0" * 4096)
        fcntl.ioctl(fd, 0x90044802, buf)  # HIDIOCGRDESC (bytearray: bytes args cap at 1024)
        return bytes(buf[4:4 + size])
    finally:
        os.close(fd)


# ---- usbip server / client plumbing ----------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    """usbipd under test, foreground, on its own TCP port. netns: run inside a network namespace."""

    def __init__(self, port=None, netns=None, host="127.0.0.1"):
        self.port, self.netns, self.host, self.proc = port or free_port(), netns, host, None

    def start(self):
        pre = ["ip", "netns", "exec", self.netns] if self.netns else []
        self.proc = subprocess.Popen(pre + USBIPD + ["-t", str(self.port)], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

        def up():
            if self.proc.poll() is not None:
                raise RuntimeError("usbipd exited")
            try:
                socket.create_connection((self.host, self.port), 0.5).close()
                return True
            except OSError:
                return False
        if not wait_for(up, 8):
            raise RuntimeError("usbipd never listened")
        return self

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def kill(self, sig=9):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            self.proc.wait(5)

    stop = kill

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.kill(15)


BIND_RETRIES = 0  # how often a bind needed a retry: stock `usbip bind` flakes while the host is still probing a new device


def bind(busid):
    global BIND_RETRIES
    assert is_test_device(busid), f"refusing to bind non-test device {busid}"
    r = sh("usbip", "bind", "-b", busid, check=False)
    for _ in range(40):
        if r.returncode == 0:
            break
        BIND_RETRIES += 1
        time.sleep(0.25)
        r = sh("usbip", "bind", "-b", busid, check=False)
    return r


def unbind(busid):
    assert is_test_device(busid)
    return sh("usbip", "unbind", "-b", busid, check=False)


def is_test_device(busid):
    try:
        return read(f"{SYS_USB}/{busid}/manufacturer") == MFR
    except OSError:
        return False


def vhci_status():
    """[(hub, port, state, local_busid)] across every vhci_hcd controller."""
    rows = []
    for f in sorted(glob.glob("/sys/devices/platform/vhci_hcd.*/status*")):
        with open(f) as fh:
            lines = fh.read().splitlines()[1:]
        for line in lines:
            c = line.split()
            if len(c) >= 7:
                rows.append((c[0], int(c[1]), int(c[2]), c[6]))
    return rows


def vhci_used_ports():
    return {(h, p): b for h, p, st, b in vhci_status() if st == 6}


def wait_available(busid, timeout=8):
    """Server side is ready to export again (usbip-host status 1) after a session ends."""
    f = f"/sys/bus/usb/drivers/usbip-host/{busid}/usbip_status"
    return wait_for(lambda: os.path.exists(f) and read(f) == "1", timeout)


def attach(server, busid, timeout=8):
    """Import over the kernel client. Returns (vhci_port, local_busid)."""
    before = vhci_used_ports()
    err = ""
    for _ in range(int(timeout / 0.25)):  # a device that just ended a session is briefly busy
        try:  # bounded: stock `usbip attach` livelocks when several run at once (see test_50)
            r = sh(*USBIP, "-t", str(server.port), "attach", "-r", server.host, "-b", busid, check=False, timeout=15)
        except subprocess.TimeoutExpired:
            raise RuntimeError("attach hung (killed after 15 s)")
        if r.returncode == 0:
            break
        err = r.stderr.strip() or r.stdout.strip()
        time.sleep(0.25)
    else:
        raise RuntimeError(f"attach failed: {err}")

    def new():
        d = [(k, v) for k, v in vhci_used_ports().items() if k not in before]
        return d[0] if d else None
    got = wait_for(new, timeout)
    if not got:
        raise RuntimeError("attach reported OK but no vhci port went busy")
    (_, port), local = got
    return port, local


def detach(port):
    return sh(*USBIP, "detach", "-p", str(port), check=False)


def kernel_log_since(mark):
    out = sh("journalctl", "-k", "--since", mark, "--no-pager", "-o", "cat", check=False).stdout
    return [l for l in out.splitlines() if re.search(r"BUG:|Oops|WARNING:|general protection|KASAN|use-after-free|soft lockup|usbip.*(error|fail)", l, re.I)
            and "UFW" not in l]


def now_mark():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---- raw USB/IP protocol client (what any server must answer) ----------------------------------------

VER = 0x0111
OP_REQ_DEVLIST, OP_REP_DEVLIST, OP_REQ_IMPORT, OP_REP_IMPORT = 0x8005, 0x0005, 0x8003, 0x0003
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4
DEVFMT = ">256s32sIIIHHHBBBBBB"
DEVSZ = struct.calcsize(DEVFMT)  # 312
DEVKEYS = ("path", "busid", "busnum", "devnum", "speed", "vid", "pid", "bcd", "dclass", "dsub", "dproto",
           "cfgval", "ncfg", "nif")


def _dev(b):
    d = dict(zip(DEVKEYS, struct.unpack(DEVFMT, b)))
    d["path"], d["busid"] = d["path"].split(b"\0")[0].decode(), d["busid"].split(b"\0")[0].decode()
    return d


class Raw:
    def __init__(self, host, port, timeout=3.0):
        self.s = socket.create_connection((host, port), timeout)
        self.seq = 0
        self.pending = {}  # seqnum -> direction

    def close(self):
        self.s.close()

    def send(self, b):
        self.s.sendall(b)

    def recv(self, n):
        buf = b""
        while len(buf) < n:
            c = self.s.recv(n - len(buf))
            if not c:
                raise EOFError(f"closed after {len(buf)}/{n} bytes")
            buf += c
        return buf

    def devlist(self):
        self.send(struct.pack(">HHI", VER, OP_REQ_DEVLIST, 0))
        ver, code, status = struct.unpack(">HHI", self.recv(8))
        assert (ver, code, status) == (VER, OP_REP_DEVLIST, 0), (hex(ver), hex(code), status)
        (n,) = struct.unpack(">I", self.recv(4))
        out = []
        for _ in range(n):
            d = _dev(self.recv(DEVSZ))
            d["ifaces"] = [struct.unpack(">BBBB", self.recv(4)) for _ in range(d["nif"])]
            out.append(d)
        return out

    def import_(self, busid):
        self.send(struct.pack(">HHI32s", VER, OP_REQ_IMPORT, 0, busid.encode()))
        ver, code, status = struct.unpack(">HHI", self.recv(8))
        assert (ver, code) == (VER, OP_REP_IMPORT), (hex(ver), hex(code))
        return status, (_dev(self.recv(DEVSZ)) if status == 0 else None)

    def _hdr(self, cmd, devid, direction, ep):
        self.seq += 1
        return self.seq, struct.pack(">IIIII", cmd, self.seq, devid, direction, ep)

    def submit(self, devid, ep, direction, length, setup=b"\0" * 8, payload=b"", interval=0, flags=0):
        seq, h = self._hdr(CMD_SUBMIT, devid, direction, ep)
        h += struct.pack(">Iiiii8s", flags, length, 0, -1, interval, setup)
        self.pending[seq] = direction
        self.send(h + payload)
        return seq

    def unlink(self, devid, victim):
        seq, h = self._hdr(CMD_UNLINK, devid, 0, 0)
        self.send(h + struct.pack(">I", victim) + b"\0" * 24)
        return seq

    def reply(self):
        """-> dict(cmd, seq, status, actual, data)"""
        h = self.recv(48)
        cmd, seq = struct.unpack(">II", h[:8])
        r = {"cmd": cmd, "seq": seq, "status": struct.unpack(">i", h[20:24])[0], "actual": 0, "data": b""}
        if cmd == RET_SUBMIT:
            r["actual"] = struct.unpack(">i", h[24:28])[0]
            if self.pending.pop(seq, 0) == 1 and r["actual"] > 0:
                r["data"] = self.recv(r["actual"])
        return r

    def ctl_in(self, devid, setup, length):
        return self.submit(devid, 0, 1, length, setup=setup)


def get_descriptor_setup(dtype, index, length, iface=False, value_lang=0):
    rt = 0x81 if iface else 0x80
    return struct.pack("<BBHHH", rt, 6, (dtype << 8) | index, value_lang, length)


# ---- report streaming, latency measurement -----------------------------------------------------------

def _writer(hidg, n, hz, ready, idx, size):
    fd = os.open(hidg, os.O_WRONLY)
    ready.wait(15)
    time.sleep(0.3)  # let the reader's interrupt URBs reach the device
    period, t0 = 1.0 / hz, time.perf_counter()
    for i in range(n):
        target = t0 + i * period
        d = target - time.perf_counter()
        if d > 0:
            time.sleep(d)
        os.write(fd, struct.pack("<IQI", i, time.monotonic_ns(), idx).ljust(size, b"\0"))
    os.close(fd)


def _reader(hidraw, n, hz, ready, conn):
    fd = os.open(hidraw, os.O_RDONLY | os.O_NONBLOCK)
    ready.set()
    rows, deadline = [], time.monotonic() + n / hz + 6
    while len(rows) < n and time.monotonic() < deadline:
        if not select.select([fd], [], [], 0.25)[0]:
            continue
        try:
            b = os.read(fd, 64)
        except OSError:
            break
        t = time.monotonic_ns()
        if len(b) >= 16:
            seq, ts, _ = struct.unpack("<IQI", b[:16])
            rows.append((seq, t - ts, t))
    os.close(fd)
    conn.send(rows)


def stream(pairs, seconds, hz, size=16):
    """Pace `hz` reports/s through every (hidg, hidraw) pair at once; return one stats dict per pair."""
    n, ctx, jobs = int(seconds * hz), mp.get_context("fork"), []
    for i, (hidg, hidraw) in enumerate(pairs):
        ready, (pc, cc) = ctx.Event(), ctx.Pipe(False)
        r = ctx.Process(target=_reader, args=(hidraw, n, hz, ready, cc))
        w = ctx.Process(target=_writer, args=(hidg, n, hz, ready, i, size))
        r.start()
        w.start()
        jobs.append((r, w, pc))
    out = []
    for r, w, pc in jobs:
        rows = pc.recv() if pc.poll(seconds + 20) else []
        r.join(5)
        w.join(5)
        for p in (r, w):
            if p.is_alive():
                p.kill()
        out.append(analyze(rows, n))
    return out


def analyze(rows, sent):
    seqs = [r[0] for r in rows]
    uniq = set(seqs)
    lat = sorted(r[1] / 1e6 for r in rows)
    q = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] if lat else float("nan")
    out = {"sent": sent, "got": len(rows), "lost": sent - len(uniq), "dup": len(seqs) - len(uniq),
           "reorder": sum(1 for a, b in zip(seqs, seqs[1:]) if b < a),
           "p50": q(.5), "p95": q(.95), "p99": q(.99), "max": lat[-1] if lat else float("nan"),
           "mean": sum(lat) / len(lat) if lat else float("nan")}
    if len(rows) > 1 and len(rows[0]) > 2 and rows[-1][2] > rows[0][2]:
        out["rate"] = (len(rows) - 1) / ((rows[-1][2] - rows[0][2]) / 1e9)  # delivered reports per second
    return out


def merge(stats):
    """Worst case across devices (lowest rate, highest everything else)."""
    return {k: (min(s[k] for s in stats) if k == "rate" else
                sum(s[k] for s in stats) if k in ("sent", "got") else max(s[k] for s in stats))
            for k in stats[0]}


def record(name, **fields):
    os.makedirs(RESULTS, exist_ok=True)
    row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "test": name, "server": " ".join(USBIPD), **fields}
    with open(os.path.join(RESULTS, "results.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"\n  [{name}] " + " ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in fields.items()))


# ---- loopback devices: bulk echo through usbfs, round-trip timing --------------------------------------

USBDEVFS_BULK, USBDEVFS_CLAIM, USBDEVFS_SETCONFIG = 0xC0185502, 0x8004550F, 0x80045505


def _bulk(fd, ep, buf, timeout=1000):
    """One synchronous bulk transfer on a bytearray. Returns bytes moved."""
    raw = (ctypes.c_char * len(buf)).from_buffer(buf)
    arg = bytearray(struct.pack("<IIIxxxxQ", ep, len(buf), timeout, ctypes.addressof(raw)))  # struct usbdevfs_bulktransfer
    return fcntl.ioctl(fd, USBDEVFS_BULK, arg)


def open_loop(busid):
    """usbfs handle with configuration 1 selected and interface 0 claimed. -> (fd, ep_out, ep_in)"""
    def node():  # enumeration over a slow link takes a while: wait for the device node, not just the vhci port
        try:
            bus, dev = int(read(f"{SYS_USB}/{busid}/busnum")), int(read(f"{SYS_USB}/{busid}/devnum"))
        except (OSError, ValueError):
            return None
        p = f"/dev/bus/usb/{bus:03d}/{dev:03d}"
        return p if os.path.exists(p) else None
    path = wait_for(node, 15)
    if not path:
        raise RuntimeError(f"{busid}: device node never appeared")
    fd = os.open(path, os.O_RDWR)
    fcntl.ioctl(fd, USBDEVFS_SETCONFIG, struct.pack("I", 1))
    eps = {}

    def found():
        for e in glob.glob(f"{SYS_USB}/{busid}/{busid}:1.0/ep_*"):
            if read(e + "/type") == "Bulk":
                eps[read(e + "/direction")] = int(os.path.basename(e)[3:], 16)
        return len(eps) == 2
    if not wait_for(found, 15):
        raise RuntimeError(f"{busid}: loopback endpoints never appeared")
    fcntl.ioctl(fd, USBDEVFS_CLAIM, struct.pack("I", 0))
    return fd, eps["out"], eps["in"]


def _loop_worker(busid, n, hz, go, conn, idx):
    rows = []
    try:
        fd, eo, ei = open_loop(busid)
        go.wait(30)
        period, t0 = 1.0 / hz, time.perf_counter()
        for i in range(n):
            d = t0 + i * period - time.perf_counter()
            if d > 0:
                time.sleep(d)
            t = time.monotonic_ns()
            out = bytearray(struct.pack("<IQI", i, t, idx))
            try:
                _bulk(fd, eo, out)
                back = bytearray(16)
                _bulk(fd, ei, back)
            except OSError:
                continue
            if bytes(back) == bytes(out):
                rows.append((i, time.monotonic_ns() - t))
        os.close(fd)
    except Exception as e:  # report as total loss, the test names the cause
        print("loop worker", busid, "failed:", e)
    conn.send(rows)


def loop_stream(busids, seconds, hz):
    """Echo `hz` bulk round trips/s on every loopback device at once. Latency here is a full round trip."""
    n, ctx = int(seconds * hz), mp.get_context("fork")
    go, jobs = ctx.Event(), []
    for i, b in enumerate(busids):
        pc, cc = ctx.Pipe(False)
        p = ctx.Process(target=_loop_worker, args=(b, n, hz, go, cc, i))
        p.start()
        jobs.append((p, pc))
    time.sleep(1.0 + 0.1 * len(busids))  # let every worker finish setup
    go.set()
    out = []
    for p, pc in jobs:
        rows = pc.recv() if pc.poll(seconds + 30) else []
        p.join(5)
        if p.is_alive():
            p.kill()
        out.append(analyze(rows, n))
    return out


def fmt(s):
    r = f" rate={s['rate']:.0f}/s" if "rate" in s else ""
    return f"p50={s['p50']:.2f} p99={s['p99']:.2f} max={s['max']:.2f}ms lost={s['lost']}/{s['sent']}{r}"


fuzz_rng = random.Random(int(os.environ.get("FUZZ_SEED", "1")))


def cleanup():
    """Detach every imported device, unbind every test device, remove every test gadget."""
    for (_, port) in list(vhci_used_ports()):
        detach(port)
    for d in glob.glob(SYS_USB + "/[0-9]*-[0-9]*"):
        b = os.path.basename(d)
        if ":" not in b and is_test_device(b):
            sh("usbip", "unbind", "-b", b, check=False)  # device may vanish mid-cleanup: no asserts here
    destroy_all_gadgets()


class Link:
    """The server in its own network namespace, joined to this one by a veth pair. shape() applies netem to both
    directions of that pair only, so nothing else on the machine is affected."""
    NS, HOST_IF, NS_IF, HOST_IP, NS_IP, NET = "ufk-srv", "ufk0", "ufk1", "10.99.0.1", "10.99.0.2", "10.99.0.0/24"

    def __init__(self):
        self.fw = None

    def up(self):
        import unittest
        try:
            sh("ip", "netns", "del", self.NS, check=False)
            sh("ip", "netns", "add", self.NS)
            sh("ip", "link", "add", self.HOST_IF, "type", "veth", "peer", "name", self.NS_IF)
            sh("ip", "link", "set", self.NS_IF, "netns", self.NS)
            sh("ip", "addr", "add", f"{self.HOST_IP}/24", "dev", self.HOST_IF)
            sh("ip", "link", "set", self.HOST_IF, "up")
            sh("ip", "-n", self.NS, "addr", "add", f"{self.NS_IP}/24", "dev", self.NS_IF)
            sh("ip", "-n", self.NS, "link", "set", self.NS_IF, "up")
            sh("ip", "-n", self.NS, "link", "set", "lo", "up")
            sh("modprobe", "sch_netem")
            # a host firewall (ufw) would drop traffic arriving on the new interface; allow just this one
            if sh("which", "iptables", check=False).returncode == 0:
                self.fw = ["iptables", "-I", "INPUT", "-i", self.HOST_IF, "-j", "ACCEPT"]
                sh(*self.fw)
            if os.environ.get("RTO_MIN_MS"):
                self.rto(os.environ["RTO_MIN_MS"])
            sh("ping", "-c", "1", "-W", "2", self.NS_IP)
        except (subprocess.CalledProcessError, OSError) as e:
            self.down()
            raise unittest.SkipTest(f"cannot build the test network: {getattr(e, 'stderr', e)}")
        return self

    def rto(self, ms):
        """Lower the TCP minimum retransmit timeout on both ends (the loss-stall experiment)."""
        sh("ip", "route", "change", self.NET, "dev", self.HOST_IF, "rto_min", f"{ms}ms")
        sh("ip", "-n", self.NS, "route", "change", self.NET, "dev", self.NS_IF, "rto_min", f"{ms}ms")

    def shape(self, spec):
        """spec: list of netem args, or None to clear."""
        for dev, ns in ((self.HOST_IF, None), (self.NS_IF, self.NS)):
            pre = ["ip", "netns", "exec", ns] if ns else []
            if spec is None:
                sh(*pre, "tc", "qdisc", "del", "dev", dev, "root", check=False)
            else:
                sh(*pre, "tc", "qdisc", "replace", "dev", dev, "root", "netem", *spec)

    def down(self):
        if self.fw:
            sh(*(["iptables", "-D"] + self.fw[2:]), check=False)
            self.fw = None
        sh("ip", "netns", "del", self.NS, check=False)  # removes the veth pair with it


class Base(__import__("unittest").TestCase):
    """Fails the class if the kernel logged an oops/warning/usbip error while it ran."""

    @classmethod
    def setUpClass(cls):
        cls._mark = now_mark()
        setup_modules()

    @classmethod
    def tearDownClass(cls):
        bad = kernel_log_since(cls._mark)
        if bad:
            raise AssertionError("kernel log:\n" + "\n".join(bad[:10]))


if __name__ == "__main__":
    cleanup()
    print("cleaned up")

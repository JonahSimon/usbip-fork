//! ufk: prototype USB/IP server and Linux client, std only.
//!   ufk serve  [--port 3241] [--hz 270] [--devices 1] [--stats 5] [--no-nodelay]
//!   ufk attach HOST:PORT BUSID          (Linux, root: hands the connected socket to vhci_hcd)
//! The server exports synthetic HID devices (busid 1-1, 1-2, ...) so the transport can be tested over a real network
//! without hardware. Wire format is the stock USB/IP protocol, so stock `usbip attach` works against it too.
//! Input report (16 B): [0]=1 stream | 2 echo, [4..8]=seq, [8..16]=server ns (stream) or the 8 bytes the host wrote (echo).
//! Latency policy: a stream report is delivered the moment a URB is waiting, otherwise only the NEWEST is kept
//! (latest wins, nothing queues up and goes stale). Echo reports are never dropped and go first.
use std::collections::VecDeque;
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const VER: u16 = 0x0111;
const EPIPE: i32 = -32;
const ECONNRESET: i32 = -104;
const VID: u16 = 0x1209;
const PID: u16 = 0x0002;
const MAX_URB: usize = 1 << 20;

const REPORT_DESC: &[u8] = &[
    0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0x09, 0x02, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x10, 0x81,
    0x02, 0x09, 0x03, 0x95, 0x08, 0x91, 0x02, 0xC0,
];

fn device_desc() -> Vec<u8> {
    vec![18, 1, 0, 2, 0, 0, 0, 64, VID as u8, (VID >> 8) as u8, PID as u8, (PID >> 8) as u8, 0, 1, 1, 2, 3, 1]
}

fn config_desc() -> Vec<u8> {
    let mut d = vec![9, 2, 41, 0, 1, 1, 0, 0x80, 50];
    d.extend([9, 4, 0, 0, 2, 3, 0, 0, 0]);
    d.extend([9, 0x21, 0x11, 0x01, 0, 1, 0x22, REPORT_DESC.len() as u8, 0]);
    d.extend([7, 5, 0x81, 3, 64, 0, 4]);
    d.extend([7, 5, 0x02, 3, 64, 0, 4]);
    d
}

fn string_desc(idx: u32, i: u8) -> Option<Vec<u8>> {
    let s = match i {
        0 => return Some(vec![4, 3, 9, 4]),
        1 => "usbip-fork-demo".to_string(),
        2 => "ufk demo gamepad".to_string(),
        3 => format!("ufkdemo{idx}"),
        _ => return None,
    };
    let u: Vec<u8> = s.encode_utf16().flat_map(|c| c.to_le_bytes()).collect();
    let mut d = vec![(u.len() + 2) as u8, 3];
    d.extend(u);
    Some(d)
}

fn be32(b: &[u8]) -> u32 {
    u32::from_be_bytes(b[..4].try_into().unwrap())
}

fn ret_submit(seq: u32, status: i32, actual: i32, data: &[u8]) -> Vec<u8> {
    let mut b = Vec::with_capacity(48 + data.len());
    b.extend(3u32.to_be_bytes());
    b.extend(seq.to_be_bytes());
    b.extend([0u8; 12]); // devid, direction, ep
    b.extend(status.to_be_bytes());
    b.extend(actual.to_be_bytes());
    b.extend([0u8; 12]); // start_frame, number_of_packets (0: not isochronous), error_count
    b.extend([0u8; 8]); // setup padding
    b.extend(data);
    b
}

fn ret_unlink(seq: u32, status: i32) -> Vec<u8> {
    let mut b = Vec::with_capacity(48);
    b.extend(4u32.to_be_bytes());
    b.extend(seq.to_be_bytes());
    b.extend([0u8; 12]);
    b.extend(status.to_be_bytes());
    b.extend([0u8; 24]);
    b
}

fn dev_record(idx: u32, with_ifaces: bool) -> Vec<u8> {
    let mut b = Vec::new();
    let mut path = [0u8; 256];
    let p = format!("/sys/devices/ufk/usb1/1-{idx}");
    path[..p.len()].copy_from_slice(p.as_bytes());
    b.extend(path);
    let mut bus = [0u8; 32];
    let bs = format!("1-{idx}");
    bus[..bs.len()].copy_from_slice(bs.as_bytes());
    b.extend(bus);
    for v in [1u32, idx, 3] {
        b.extend(v.to_be_bytes()); // busnum, devnum, speed (3 = high)
    }
    b.extend(VID.to_be_bytes());
    b.extend(PID.to_be_bytes());
    b.extend(0x0100u16.to_be_bytes());
    b.extend([0, 0, 0, 1, 1, 1]); // class, subclass, protocol, cfg value, num configs, num interfaces
    if with_ifaces {
        b.extend([3, 0, 0, 0]);
    }
    b
}

struct Conn(Mutex<TcpStream>);
impl Conn {
    fn send(&self, b: &[u8]) {
        let _ = self.0.lock().unwrap().write_all(b);
    }
}

#[derive(Default)]
struct Stats {
    delivered: u64,
    coalesced: u64,
    echoes: u64,
    ages_ms: Vec<f32>,
}

#[derive(Default)]
struct St {
    conn: Option<Arc<Conn>>,
    pending: VecDeque<u32>,
    latest: Option<(Instant, [u8; 16])>,
    echo: VecDeque<[u8; 16]>,
    seq: u32,
    echo_seq: u32,
    stats: Stats,
}

struct Dev {
    idx: u32,
    st: Mutex<St>,
}

fn send_in(conn: &Conn, seq: u32, r: &[u8; 16]) {
    conn.send(&ret_submit(seq, 0, 16, r));
}

fn push_echo(st: &mut St, out: &[u8]) {
    let Some(conn) = st.conn.clone() else { return };
    st.echo_seq = st.echo_seq.wrapping_add(1);
    let mut r = [0u8; 16];
    r[0] = 2;
    r[4..8].copy_from_slice(&st.echo_seq.to_le_bytes());
    let n = out.len().min(8);
    r[8..8 + n].copy_from_slice(&out[..n]);
    st.stats.echoes += 1;
    match st.pending.pop_front() {
        Some(seq) => send_in(&conn, seq, &r),
        None => st.echo.push_back(r),
    }
}

fn generator(devs: Arc<Vec<Arc<Dev>>>, hz: f64) {
    let period = Duration::from_secs_f64(1.0 / hz);
    let t0 = Instant::now();
    let mut n: u32 = 0;
    loop {
        n += 1;
        let deadline = t0 + period * n;
        loop {
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            let left = deadline - now;
            if left > Duration::from_micros(500) {
                std::thread::sleep(left - Duration::from_micros(400));
            } else {
                std::hint::spin_loop(); // ponytail: spins the last 0.4 ms; one core busy while serving
            }
        }
        let now = Instant::now();
        for d in devs.iter() {
            let mut st = d.st.lock().unwrap();
            let Some(conn) = st.conn.clone() else { continue };
            st.seq = st.seq.wrapping_add(1);
            let mut r = [0u8; 16];
            r[0] = 1;
            r[4..8].copy_from_slice(&st.seq.to_le_bytes());
            r[8..16].copy_from_slice(&(t0.elapsed().as_nanos() as u64).to_le_bytes());
            if let Some(seq) = st.pending.pop_front() {
                send_in(&conn, seq, &r);
                st.stats.delivered += 1;
                st.stats.ages_ms.push(0.0);
            } else {
                if st.latest.is_some() {
                    st.stats.coalesced += 1;
                }
                st.latest = Some((now, r));
            }
        }
    }
}

fn stats_loop(devs: Arc<Vec<Arc<Dev>>>, secs: u64) {
    loop {
        std::thread::sleep(Duration::from_secs(secs));
        for d in devs.iter() {
            let mut st = d.st.lock().unwrap();
            if st.conn.is_none() {
                continue;
            }
            let mut s = std::mem::take(&mut st.stats);
            s.ages_ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let q = |p: f64| s.ages_ms.get(((s.ages_ms.len() as f64 * p) as usize).min(s.ages_ms.len().saturating_sub(1))).copied().unwrap_or(0.0);
            println!(
                "[ufk] dev 1-{} delivered={} coalesced={} echoes={} wait_ms p50={:.2} p99={:.2} max={:.2}",
                d.idx, s.delivered, s.coalesced, s.echoes, q(0.5), q(0.99), q(1.0)
            );
        }
        let _ = io::stdout().flush();
    }
}

fn control(dev: &Dev, s: &[u8; 8], out: &[u8]) -> (i32, Vec<u8>) {
    let ok = |v: Vec<u8>| (0, v);
    let stall = (EPIPE, vec![]);
    let val = u16::from_le_bytes([s[2], s[3]]);
    match (s[0], s[1]) {
        (0x80, 6) => match val >> 8 {
            1 => ok(device_desc()),
            2 => ok(config_desc()),
            3 => string_desc(dev.idx, val as u8).map_or(stall, ok),
            _ => stall,
        },
        (0x81, 6) => match val >> 8 {
            0x22 => ok(REPORT_DESC.to_vec()),
            0x21 => ok(config_desc()[18..27].to_vec()),
            _ => stall,
        },
        (0x00, 9) | (0x01, 11) | (0x00, 1) | (0x00, 3) | (0x01, 1) | (0x01, 3) | (0x21, 0x0a) | (0x21, 0x0b) => ok(vec![]),
        (0x80, 8) => ok(vec![1]),
        (0x80, 0) | (0x81, 0) => ok(vec![0, 0]),
        (0x21, 0x09) => {
            push_echo(&mut dev.st.lock().unwrap(), out);
            ok(vec![])
        }
        (0xa1, 0x01) => ok(dev.st.lock().unwrap().latest.map_or(vec![0; 16], |(_, r)| r.to_vec())),
        _ => stall,
    }
}

fn submit(conn: &Arc<Conn>, dev: &Dev, seq: u32, ep: u32, dir: u32, len: usize, setup: [u8; 8], data: Vec<u8>) {
    if ep == 0 {
        let (status, resp) = control(dev, &setup, &data);
        let n = resp.len().min(len);
        if dir == 1 {
            conn.send(&ret_submit(seq, status, n as i32, &resp[..n]));
        } else {
            conn.send(&ret_submit(seq, status, if status == 0 { len as i32 } else { 0 }, &[]));
        }
    } else if ep == 1 && dir == 1 {
        let mut st = dev.st.lock().unwrap();
        if let Some(r) = st.echo.pop_front() {
            send_in(conn, seq, &r);
        } else if let Some((t, r)) = st.latest.take() {
            send_in(conn, seq, &r);
            st.stats.delivered += 1;
            st.stats.ages_ms.push(t.elapsed().as_secs_f32() * 1000.0);
        } else {
            st.pending.push_back(seq);
        }
    } else if ep == 2 && dir == 0 {
        conn.send(&ret_submit(seq, 0, len as i32, &[]));
        push_echo(&mut dev.st.lock().unwrap(), &data);
    } else {
        conn.send(&ret_submit(seq, EPIPE, 0, &[]));
    }
}

fn urb_loop(mut s: TcpStream, dev: Arc<Dev>) -> io::Result<()> {
    let conn = Arc::new(Conn(Mutex::new(s.try_clone()?)));
    *dev.st.lock().unwrap() = St { conn: Some(conn.clone()), ..Default::default() };
    let res = (|| -> io::Result<()> {
        loop {
            let mut h = [0u8; 48];
            s.read_exact(&mut h)?;
            let (cmd, seq, dir, ep) = (be32(&h[0..4]), be32(&h[4..8]), be32(&h[12..16]), be32(&h[16..20]));
            match cmd {
                1 => {
                    let len = i32::from_be_bytes(h[24..28].try_into().unwrap()).max(0) as usize;
                    if len > MAX_URB {
                        return Err(io::Error::other("URB too large"));
                    }
                    let setup: [u8; 8] = h[40..48].try_into().unwrap();
                    let mut data = vec![0u8; if dir == 0 { len } else { 0 }];
                    s.read_exact(&mut data)?;
                    submit(&conn, &dev, seq, ep, dir, len, setup, data);
                }
                2 => {
                    let victim = be32(&h[20..24]);
                    let mut st = dev.st.lock().unwrap();
                    let status = match st.pending.iter().position(|&x| x == victim) {
                        Some(i) => {
                            st.pending.remove(i);
                            ECONNRESET
                        }
                        None => 0,
                    };
                    conn.send(&ret_unlink(seq, status));
                }
                _ => return Ok(()),
            }
        }
    })();
    *dev.st.lock().unwrap() = St::default();
    res
}

fn session(mut s: TcpStream, devs: Arc<Vec<Arc<Dev>>>, nodelay: bool) -> io::Result<()> {
    s.set_nodelay(nodelay)?;
    s.set_read_timeout(Some(Duration::from_secs(10)))?; // handshake only
    let mut h = [0u8; 8];
    s.read_exact(&mut h)?;
    if u16::from_be_bytes([h[0], h[1]]) != VER {
        return Ok(());
    }
    match u16::from_be_bytes([h[2], h[3]]) {
        0x8005 => {
            let mut r = Vec::new();
            r.extend(VER.to_be_bytes());
            r.extend(5u16.to_be_bytes());
            r.extend(0u32.to_be_bytes());
            r.extend((devs.len() as u32).to_be_bytes());
            for d in devs.iter() {
                r.extend(dev_record(d.idx, true));
            }
            s.write_all(&r)
        }
        0x8003 => {
            let mut b = [0u8; 32];
            s.read_exact(&mut b)?;
            let want = String::from_utf8_lossy(b.split(|&c| c == 0).next().unwrap()).to_string();
            let dev = devs.iter().find(|d| format!("1-{}", d.idx) == want).cloned();
            let mut r = Vec::new();
            r.extend(VER.to_be_bytes());
            r.extend(3u16.to_be_bytes());
            match dev {
                Some(d) if d.st.lock().unwrap().conn.is_none() => {
                    r.extend(0u32.to_be_bytes());
                    r.extend(dev_record(d.idx, false));
                    s.write_all(&r)?;
                    s.set_read_timeout(None)?;
                    urb_loop(s, d)
                }
                _ => {
                    r.extend(1u32.to_be_bytes());
                    s.write_all(&r)
                }
            }
        }
        _ => Ok(()),
    }
}

fn opt<T: std::str::FromStr>(a: &[String], name: &str, default: T) -> T {
    a.iter().position(|x| x == name).and_then(|i| a.get(i + 1)).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn serve(a: &[String]) {
    let (port, hz, n, stats): (u16, f64, u32, u64) = (opt(a, "--port", 3241), opt(a, "--hz", 270.0), opt(a, "--devices", 1), opt(a, "--stats", 5));
    let nodelay = !a.iter().any(|x| x == "--no-nodelay");
    let devs: Arc<Vec<Arc<Dev>>> = Arc::new((1..=n).map(|idx| Arc::new(Dev { idx, st: Mutex::new(St::default()) })).collect());
    let l = TcpListener::bind(("0.0.0.0", port)).expect("bind");
    println!("[ufk] serving {n} demo device(s) at {hz} Hz on port {port}, nodelay={nodelay}");
    let _ = io::stdout().flush();
    let (g, s) = (devs.clone(), devs.clone());
    std::thread::spawn(move || generator(g, hz));
    if stats > 0 {
        std::thread::spawn(move || stats_loop(s, stats));
    }
    for c in l.incoming().flatten() {
        let d = devs.clone();
        std::thread::spawn(move || {
            let _ = session(c, d, nodelay);
        });
    }
}

#[cfg(target_os = "linux")]
fn attach(addr: &str, busid: &str) -> io::Result<()> {
    // The kernel can accept the socket yet never enumerate the device when a port is reused right after a detach.
    // So: verify the port reaches USED, and if not, detach it and start over on a fresh connection.
    let mut last = io::Error::other("attach failed");
    for attempt in 1..=4 {
        match attach_once(addr, busid) {
            Ok(port) => {
                if attempt > 1 {
                    println!("(attach needed {attempt} attempts)");
                }
                println!("attached {busid} on vhci port {port}");
                return Ok(());
            }
            Err(e) if e.kind() == io::ErrorKind::PermissionDenied || e.kind() == io::ErrorKind::ConnectionRefused => return Err(e),
            Err(e) => last = e,
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    Err(last)
}

#[cfg(target_os = "linux")]
fn attach_once(addr: &str, busid: &str) -> io::Result<u32> {
    use std::os::fd::AsRawFd;
    const VHCI: &str = "/sys/devices/platform/vhci_hcd.0";
    let mut s = TcpStream::connect(addr)?;
    s.set_nodelay(true)?;
    let mut req = Vec::new();
    req.extend(VER.to_be_bytes());
    req.extend(0x8003u16.to_be_bytes());
    req.extend(0u32.to_be_bytes());
    let mut b = [0u8; 32];
    b[..busid.len().min(31)].copy_from_slice(&busid.as_bytes()[..busid.len().min(31)]);
    req.extend(b);
    s.write_all(&req)?;
    let mut h = [0u8; 8];
    s.read_exact(&mut h)?;
    if be32(&h[4..8]) != 0 {
        return Err(io::Error::new(io::ErrorKind::AddrInUse, "import refused (device busy or unknown)"));
    }
    let mut d = [0u8; 312];
    s.read_exact(&mut d)?;
    let (busnum, devnum, speed) = (be32(&d[288..]), be32(&d[292..]), be32(&d[296..]));
    let status = std::fs::read_to_string(format!("{VHCI}/status"))?;
    let state = |port: u32| -> Option<String> {
        let st = std::fs::read_to_string(format!("{VHCI}/status")).ok()?;
        st.lines().skip(1).find_map(|l| {
            let c: Vec<&str> = l.split_whitespace().collect();
            (c.len() >= 3 && c[0] == "hs" && c[1].parse::<u32>().ok() == Some(port)).then(|| c[2].to_string())
        })
    };
    // first free high-speed port; on a race the kernel refuses the write and we try the next one
    for l in status.lines().skip(1) {
        let c: Vec<&str> = l.split_whitespace().collect();
        if c.len() < 3 || c[0] != "hs" || c[2] != "004" {
            continue;
        }
        let port: u32 = c[1].parse().unwrap_or(0);
        let line = format!("{} {} {} {}", port, s.as_raw_fd(), (busnum << 16) | devnum, speed);
        if std::fs::write(format!("{VHCI}/attach"), line).is_err() {
            continue;
        }
        let t = Instant::now();
        while t.elapsed() < Duration::from_secs(3) {
            if state(port).as_deref() == Some("006") {
                return Ok(port);
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = std::fs::write(format!("{VHCI}/detach"), port.to_string());
        return Err(io::Error::new(io::ErrorKind::TimedOut, "kernel accepted the socket but never enumerated the device"));
    }
    Err(io::Error::other("no free vhci port"))
}

#[cfg(not(target_os = "linux"))]
fn attach(_: &str, _: &str) -> io::Result<()> {
    Err(io::Error::other("attach is Linux-only (Windows client: use usbip-win2 for now)"))
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    match a.get(1).map(String::as_str) {
        Some("serve") => serve(&a[2..]),
        Some("attach") if a.len() == 4 => {
            if let Err(e) = attach(&a[2], &a[3]) {
                eprintln!("ufk: {e}");
                std::process::exit(1);
            }
        }
        _ => {
            eprintln!("usage: ufk serve [--port N] [--hz N] [--devices N] [--stats SECS] [--no-nodelay]\n       ufk attach HOST:PORT BUSID");
            std::process::exit(2);
        }
    }
}

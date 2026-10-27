import sys, os, subprocess, time, socket, threading, signal
sys.path.insert(0, "/home/jonah/QuickAccess/Projects/usbip-fork/tests")
import lib
UFK = "/home/jonah/QuickAccess/Projects/usbip-fork/ufk/target/release/ufk"
HOST, PORT, SINK, PCIP = "192.168.1.200", 3241, 5001, "192.168.1.218"
N, MIN, MBPS, OUT = 8, float(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
os.makedirs(OUT, exist_ok=True)
def bl(cmd): return subprocess.run(["sudo", "-u", "jonah", "/tmp/bl.sh", cmd], capture_output=True, text=True, timeout=90).stdout
def up():
    try: socket.create_connection((HOST, PORT), 1).close(); return True
    except OSError: return False
def load(stop):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); pkt = b"x" * 1400
    per = int(MBPS * 1e6 / 8 / 1400 / 100); t = time.perf_counter()
    while not stop.is_set():
        for _ in range(per): s.sendto(pkt, (HOST, SINK))
        t += 0.01; d = t - time.perf_counter()
        if d > 0: time.sleep(d)
def wmi(cmdline): bl('$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine = "%s"}' % cmdline)
procs = []
bl('Get-Process ufk -ErrorAction SilentlyContinue | Stop-Process -Force; Remove-Item C:\\Users\\Jonah\\ufk.log,C:\\Users\\Jonah\\sink.log,C:\\Users\\Jonah\\tele*.log -ErrorAction SilentlyContinue')
bl('New-NetFirewallRule -DisplayName "ufk test tcp" -Direction Inbound -Protocol TCP -LocalPort 3241 -Action Allow | Out-Null; New-NetFirewallRule -DisplayName "ufk test udp" -Direction Inbound -Protocol UDP -LocalPort 5001 -Action Allow | Out-Null')
bl(r'''Set-Content C:\Users\Jonah\ping.ps1 '$p=New-Object Net.NetworkInformation.Ping; $w=[IO.StreamWriter]::new("C:\Users\Jonah\tele_ping.log"); $w.AutoFlush=$true; while($true){ $t=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); try{$r=$p.Send("%s",500)}catch{$r=$null}; if(-not $r -or $r.Status -ne "Success" -or $r.RoundtripTime -ge 30){ $w.WriteLine("P $t " + $(if($r){"$($r.Status) $($r.RoundtripTime)"}else{"ex"})) }; Start-Sleep -Milliseconds 50 }' ''' % PCIP)
bl(r'''Set-Content C:\Users\Jonah\link.ps1 '$w=[IO.StreamWriter]::new("C:\Users\Jonah\tele_link.log"); $w.AutoFlush=$true; while($true){ $t=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); $a=Get-NetAdapter -Name Wi-Fi; $w.WriteLine("L $t $($a.LinkSpeed) $($a.Status)"); Start-Sleep 2 }' ''')
bl(r'''Set-Content C:\Users\Jonah\sink.ps1 '$u=New-Object Net.Sockets.UdpClient 5001; $u.Client.ReceiveBufferSize=8MB; $ep=New-Object Net.IPEndPoint ([Net.IPAddress]::Any,0); $n=0; $t=Get-Date; while($true){ [void]$u.Receive([ref]$ep); $n++; if(((Get-Date)-$t).TotalSeconds -ge 1){ Add-Content C:\Users\Jonah\sink.log $n; $n=0; $t=Get-Date } }' ''')
try:
    for (_, pt) in list(lib.vhci_used_ports()): lib.detach(pt)
    wmi(f"cmd /c C:\\Users\\Jonah\\ufk.exe serve --port {PORT} --hz 125 --devices {N} --stats 10 > C:\\Users\\Jonah\\ufk.log 2>&1")
    wmi("powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\Jonah\\sink.ps1")
    assert lib.wait_for(up, 20, 0.3), "server not listening"
    lib.setup_modules()
    ps = [subprocess.Popen([UFK, "attach", f"{HOST}:{PORT}", f"1-{i+1}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(N)]
    for p in ps: p.wait(timeout=40)
    ports = lib.wait_for(lambda: lib.vhci_used_ports() if len(lib.vhci_used_ports()) == N else None, 15)
    assert ports, "not all pads attached"
    hr = [lib.hidraw_of(b) for _, b in sorted(ports.items())]
    # clock offset, Beelink minus PC, from the lowest-latency of 5 samples
    best = None
    for _ in range(5):
        a = time.time(); r = bl('[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()'); b = time.time()
        if r.strip().isdigit() and (best is None or b - a < best[0]): best = (b - a, int(r.strip()) / 1000 - (a + b) / 2)
    open(f"{OUT}/clock_offset.txt", "w").write(f"beelink_minus_pc_s={best[1]:.3f} sample_rtt_s={best[0]:.3f}\n")
    total = MIN * 2 * 60
    procs.append(subprocess.Popen(["tcpdump", "-i", "enp42s0", "-s", "96", "-U", "-w", f"{OUT}/cap.pcap", f"host {HOST} and tcp port {PORT}"], stderr=subprocess.DEVNULL))
    procs.append(subprocess.Popen(["ping", "-i", "0.02", "-D", "-w", str(int(total)), HOST], stdout=open(f"{OUT}/ping.log", "w"), stderr=subprocess.DEVNULL))
    wmi("powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\Jonah\\ping.ps1")
    wmi("powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\Jonah\\link.ps1")
    time.sleep(2)
    probe = subprocess.Popen(["python3", "-I", f"/home/jonah/soak/soakprobe.py", OUT, str(total)] + hr)
    open(f"{OUT}/phases.txt", "w").write(f"start={time.time():.3f}\n")
    time.sleep(MIN * 60)
    open(f"{OUT}/phases.txt", "a").write(f"load_start={time.time():.3f} mbps={MBPS}\n")
    stop = threading.Event(); th = threading.Thread(target=load, args=(stop,)); th.start()
    time.sleep(MIN * 60); stop.set(); th.join()
    open(f"{OUT}/phases.txt", "a").write(f"end={time.time():.3f}\n")
    probe.wait(timeout=30)
finally:
    for p in procs:
        try: p.send_signal(signal.SIGINT); p.wait(timeout=10)
        except Exception: pass
    for (_, pt) in list(lib.vhci_used_ports()): lib.detach(pt)
    for f in ("tele_ping.log", "tele_link.log", "sink.log", "ufk.log"):
        open(f"{OUT}/beelink_{f}", "w").write(bl(f'Get-Content C:\\Users\\Jonah\\{f} -ErrorAction SilentlyContinue | Out-String'))
    bl('Get-Process ufk -ErrorAction SilentlyContinue | Stop-Process -Force; Get-CimInstance Win32_Process -Filter "Name=\'powershell.exe\'" | ? { $_.CommandLine -match "sink.ps1|ping.ps1|link.ps1" } | % { Stop-Process -Id $_.ProcessId -Force }; Remove-NetFirewallRule -DisplayName "ufk test tcp","ufk test udp" -ErrorAction SilentlyContinue')
    open(f"{OUT}/beelink_wlan_events.txt", "w").write(bl('Get-WinEvent -FilterHashtable @{LogName="Microsoft-Windows-WLAN-AutoConfig/Operational"; StartTime=(Get-Date).AddMinutes(-MINS)} -ErrorAction SilentlyContinue | % { "{0} id={1} {2}" -f [DateTimeOffset]$_.TimeCreated, $_.Id, ($_.Message -split "`n")[0] } | Out-String'.replace('MINS', str(int(MIN * 2 + 10)))))
    open(f"{OUT}/done", "w").write("done\n")

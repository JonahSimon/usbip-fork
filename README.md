# usbip-fork

Notes and baseline tests for a possible usbip fork (VirtualHere replacement).
`ref/` holds the kernel usbip sources and protocol doc we read (GPL, from torvalds/linux master).

Status: usbip tools installed, kernel modules (`usbip-core`, `vhci-hcd`, `usbip-host`) can't load until
a reboot into 7.2.6. Plan after reboot: loopback usbipd + vhci attach of a HID device, measure round-trip
latency and polling jitter, then repeat against usbipd-win on the Beelink.

## Baseline 1: loopback, stock usbip (2026-10-05, kernel 7.2.9, usbip-utils 2.0)
Device: MSI Mystic Light `1462:7c56` (full speed HID). `bench/hidrtt.py` times 2000 GET_FEATURE round trips.

| Path | p50 | p99 | max |
|---|---|---|---|
| native hidraw | 2.999 ms | 3.021 ms | 3.056 ms |
| usbipd + vhci_hcd over 127.0.0.1 | 2.999 ms | 3.025 ms | 3.381 ms |

Read: stock usbip works and adds no visible latency on loopback, but this device itself takes ~3 ms per
report, so the test can't resolve overhead below that. Not a measure of network behavior. Next:
- a faster probe (interrupt IN on a 1000 Hz mouse or the Steam Controller puck: count events/s and gaps)
- the real test: Beelink (usbipd-win) to this PC over WiFi, 1 then 8 Xbox adapters
Setup used: `sudo usbipd -D; sudo usbip bind -b <busid>; sudo usbip attach -r 127.0.0.1 -b <busid>`;
undo with `usbip detach -p 0`, `usbip unbind -b <busid>`, `pkill usbipd`.

## Test suite (`tests/`, 2026-10-05)
Run `tests/run.sh` (about 5 min, re-execs with sudo). Pass module names to run part of it, e.g. `./run.sh test_10_protocol`.
`tests/summary.py` prints the latest numbers per test and server from `results/results.jsonl`.

**Emulated controllers:** real USB gadgets on `dummy_hcd` (configfs), so usbip can bind, export and import them like
hardware. Only devices whose manufacturer is `usbip-fork-test` are ever bound; your own devices are never touched.
- `probe`: HID with a 16-byte report carrying seq + timestamp, plus an output report. Timing is exact because device
  and host share one clock. Max 4 at once (`f_hid` has 4 minors).
- `pad`: plain HID gamepad (16 buttons, 4 axes). Checks the host sees evdev button and axis events.
- `loop`: bulk echo, no device cap. Driven through usbfs; latency is a full OUT+IN round trip.
Not emulable this way: real Xbox adapters (vendor class, not HID). Use the physical ones for a final check.

| File | What it covers |
|---|---|
| `test_10_protocol` | Raw USB/IP wire conformance: devlist, import, errors, bad version, truncated input, descriptors, pipelining, interrupt IN/OUT, unlink |
| `test_20_functional` | Kernel client end to end: identity, descriptor fidelity, stream integrity, output reports, gamepad events |
| `test_30_latency` | Native vs USB/IP loopback overhead, rate sweep 125 to 800 Hz |
| `test_40_scale` | 1, 4, 8 devices (also 8 at 1000 Hz each), vhci port cap |
| `test_50_resilience` | Unbind/detach races, server kill and restart, 100 attach/detach cycles, handshake fuzz, optional kernel URB fuzz |
| `test_60_netem` | Delay, jitter, 1% loss, 10 Mbit cap, 8 devices at 20 ms. Server in its own netns, only the veth pair is shaped |

**Same suite for the fork:** set `USBIPD_CMD` and `USBIP_CMD`. Tests marked `known_stock_bug` are expected failures on
stock and strict on anything else, so the fork must pass them.

### Baseline results, stock usbip 2.0 on kernel 7.2.9 (loopback unless noted)
- Overhead: native p50 0.07 ms, USB/IP loopback 0.09 ms (500 Hz HID). No loss, duplicates or reordering at up to 800 Hz.
- 8 devices at 1000 echo round trips/s each: p50 0.33 ms, p99 0.37 ms, zero loss.
- vhci cap: exactly 8 devices. The 9th attach fails.
- Attach plus detach cycle: about 608 ms each, steady across 100 cycles, nothing leaked.
- Netem: latency tracks the link (5 ms each way: 20.4 ms for an OUT+IN echo). **1% loss: p99 238 ms, max 273 ms**,
  because TCP waits about 200 ms to retransmit. Zero data loss, but that stall is visible as a 1/5 second input freeze.

### Stock bugs found (the fork's to-do list)
1. **Parallel attach livelock:** several `usbip attach` at once race for the same vhci port; one spins at 100% CPU while
   the kernel logs `vhci_hcd: port N already used` in a loop. (`test_parallel_attach_detach_of_several_devices`)
2. **Missed wakeup:** `stub_enqueue_ret_unlink()` never wakes the TX thread, so a RET_UNLINK for an already-finished URB
   waits for unrelated traffic. (`test_unlink_of_unknown_seqnum_is_answered`)
3. **Killing `usbipd` doesn't end sessions:** the socket belongs to the kernel after import. Good for uptime, but means
   the daemon can't revoke or meter a session. (`test_killing_the_server_process`, env `EXPECT_SESSION_SURVIVES_SERVER_KILL`)
4. `usbip bind` right after a device appears fails intermittently while the host probes it (harness retries, counts it).
5. Devlist reports zero interfaces for a bound device (it is unconfigured until the client sends SET_CONFIGURATION).
6. `usbip detach` on an empty port exits 0.
7. TCP retransmit stalls under loss (see above): an option for the fork is a lower RTO or an unreliable-with-redundancy path.

### Experiment: lower TCP rto_min on the usbip route (2026-10-05)
1% loss each way, 5 ms +/- 2 ms delay, 40 s at 40 echoes/s (1600 samples), one run per setting. Echo = OUT then IN, so about
two round trips; an input-only report pays roughly half of any stall. `RTO_MIN_MS=<n> LOSS_SECS=40 LOSS_HZ=40 ./run.sh test_60_netem`.

| rto_min | p50 ms | p95 ms | p99 ms | max ms |
|---|---|---|---|---|
| default (200) | 20.6 | 26.7 | 238.5 | 453.7 |
| 100 | 20.4 | 24.8 | 137.0 | 158.6 |
| 50 | 20.5 | 26.1 | 85.2 | 180.3 |
| 20 | 20.5 | 25.8 | 54.5 | 87.1 |
| 10 | 20.4 | 25.6 | 45.7 | 80.0 |
| 5 | 20.4 | 25.3 | 42.6 | 52.4 |

Read: the stall is TCP's retransmit timer, and `ip route change <net> dev <if> rto_min 10ms` on both ends cuts p99 about 5x
on stock usbip with no code. Single runs: max values are noisy (a second loss in a row doubles the wait). Not tested:
real WiFi (bursty loss, link-layer retries) and a Windows peer, whose minimum RTO is set differently. Also found: the test
harness must keep echo rate below 1/echo-time or the worker falls behind (100 Hz on a 20 ms echo overran the timeout).

## Emulating the Steam puck and Xbox dongle (research 2026-10-06)
**Puck (`28de:1304`).** Layout per the Linux `hid-steam` 2026 patch series and matching our `lsusb -v`: 7 interfaces. 0-1 = CDC
serial (internal comms), 2-5 = one HID interface per controller slot (up to 4), 6 = pogo-pin HID (usage page FF00, usage 2).
Slot state reports are ID 0x42, 54 bytes, about 270 per second per active slot (passive capture: 12122 in 45 s).
- `safijari/openpuck` (AGPL-3.0) is nRF52840 firmware that copies the puck; real hardware, not a Linux gadget. README gives no
  descriptors (see its ARCHITECTURE.md). Useful as a reference, and as a physical stand-in if we ever buy a $10 board.
- Linux gadget stand-in with no new code: `acm` + 4 x `f_hid` (64-byte reports, ~270 Hz) = 6 interfaces. Limit: `f_hid` has
  4 minors machine-wide, so only one such puck at a time. Two pucks or the 7th interface need FunctionFS (`usb_f_fs`, built here)
  or raw-gadget (`CONFIG_USB_RAW_GADGET=m`, available). Kernel here has ACM, FunctionFS, raw-gadget, UAC.
**Xbox dongle (`045e:02e6/02fe/02f9/091e`).** Nobody emulates it as a gadget. It is an MT76 radio the host loads firmware into
and drives with vendor bulk transfers (xone: OUT 0x04, IN 0x04 wlan and 0x05 cmd, 12 in + 12 out URBs queued, GIP data and audio
queues, driver array of 16 clients). The radio protocol (802.11-style frames, per-client keys) is too deep to fake, and driver
tests need real firmware. For usbip purposes only the shape matters: a vendor-class device with several bulk endpoints.
Reference clones saved in `ref-xone/` (xone dongle.c, mt76.c, wired.c).
Plan: puck-shaped composite via configfs now; vendor bulk multi-endpoint device for the dongle shape; real dongle and puck on the
Beelink for the final check.

#!/bin/sh
# Download the upstream sources we read while designing this (not vendored: they are GPL, from other projects).
# ref/      Linux kernel usbip: protocol doc, stub (server) and vhci (client) code, usbipd
# ref-xone/ xone (Xbox dongle driver): shows how the dongle looks on USB
set -e
cd "$(dirname "$0")"
mkdir -p ref ref-xone
L=https://raw.githubusercontent.com/torvalds/linux/master
curl -fsSL -o ref/usbip_protocol.rst "$L/Documentation/usb/usbip_protocol.rst"
for f in usbip_common.h usbip_common.c stub_rx.c stub_tx.c vhci_rx.c vhci_tx.c vhci_hcd.c; do
  curl -fsSL -o "ref/$f" "$L/drivers/usb/usbip/$f"; done
for f in usbipd.c usbip_network.c usbip_network.h; do curl -fsSL -o "ref/$f" "$L/tools/usb/usbip/src/$f"; done
X=https://raw.githubusercontent.com/dlundqvist/xone/master/transport
for f in dongle.c mt76.c mt76.h wired.c; do curl -fsSL -o "ref-xone/$f" "$X/$f"; done
echo done

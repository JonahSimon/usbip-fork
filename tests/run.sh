#!/bin/sh
# usage: ./run.sh [test_module ...]   (no args = whole suite, ~5 min). Needs root; re-execs with sudo.
# Server/client under test: USBIPD_CMD, USBIP_CMD (default: stock usbipd / usbip).
# Tuning: CYCLES, LAT_P50_MS, LAT_P99_MS, SCALE_P99_MS, EXPECT_SESSION_SURVIVES_SERVER_KILL, FUZZ_SEED, FUZZ_URB=1
cd "$(dirname "$0")" || exit 1
[ "$(id -u)" = 0 ] || exec sudo -E "$0" "$@"
python3 lib.py   # clean slate: detach, unbind, remove leftover gadgets
if [ $# -eq 0 ]; then python3 -m unittest discover -v -p 'test_*.py'; else python3 -m unittest -v "$@"; fi
rc=$?
python3 lib.py
python3 summary.py
exit $rc

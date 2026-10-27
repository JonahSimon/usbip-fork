#!/bin/bash
# usage: soak4k.sh MIN OUT -> 8-player MCC match streamed to the Beelink (Moonlight's saved settings, 4K60
# 80 Mbps) while soak.py measures 8 ufk pads (Beelink -> PC over USB/IP) for 2 x MIN minutes, all under the stream.
# Order matters: the stream goes first, because starting it (re)creates the 4K virtual screen, and moving
# gamescope windows mid-game kills MCC. Game pads are pads.py's virtual pads (run pads.py 8 first).
MIN=${1:-10}; OUT=${2:-$HOME/soak/stream4k}; mkdir -p $OUT
BL=~/soak/bl.sh
# Quit any app an earlier stream left running. Sunshine only runs its prep "do" (create the virtual
# screen) on a fresh app start; a new connection to a still-running app resumes it without one.
~/soak/ml.sh "quit 192.168.1.218" > /dev/null 2>&1; sleep 12
~/soak/ml.sh "stream 192.168.1.218 Desktop" > $OUT/ml_start.txt 2>&1
for i in $(seq 60); do kscreen-doctor -o 2>/dev/null | grep -q Virtual-sunshine && break; sleep 1; done
kscreen-doctor -o 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -A2 Virtual-sunshine > $OUT/vdisplay.txt || { echo "no virtual screen, stream did not start"; exit 1; }
sleep 5
GS_VISIBLE=1 MON=2 START=1 ~/soak/mcc/run_guide.sh > $OUT/game.out 2>&1
cp /tmp/g_*.png $OUT/ 2>/dev/null
~/soak/mcc/wiggle.sh & WG=$!
(while kill -0 $WG 2>/dev/null; do echo "$(date +%s.%N) $(cat /sys/class/net/enp42s0/statistics/tx_bytes) $(cat /sys/class/net/enp42s0/statistics/rx_bytes)"; sleep 1; done > $OUT/nic.log) &
sudo -n python3 -I ~/soak/soak.py $MIN 0 $OUT > $OUT/soak.out 2>&1
kill $WG
$BL "Stop-Process -Name Moonlight -Force; Start-Sleep 3; \$f=Get-ChildItem \$env:TEMP\Moonlight*.log | Sort LastWriteTime | Select -Last 1; Select-String -Path \$f.FullName -Pattern FFmpeg,VerticalBlank -NotMatch | Select -Expand Line | Out-String -Width 250; Unregister-ScheduledTask -TaskName ml -Confirm:\$false" > $OUT/moonlight.log 2>&1
echo SOAK4K_DONE

#!/bin/bash
# usage: sudo streamtest.sh MIN OUT -> soak.py (8 ufk pads Beelink->PC over USB/IP), Moonlight stream starts at phase 2
MIN=${1:-5}; OUT=${2:-/home/jonah/soak/stream1}; ML_ARGS=${3:-}; mkdir -p $OUT
BL="sudo -u jonah /tmp/bl.sh"
ML='"C:\Program Files\Moonlight Game Streaming\Moonlight.exe"'
python3 -I /home/jonah/soak/soak.py $MIN 0 $OUT > $OUT/soak.out 2>&1 &
SP=$!
# PC NIC counters every second, for the whole run
(while kill -0 $SP 2>/dev/null; do echo "$(date +%s.%N) $(cat /sys/class/net/enp42s0/statistics/tx_bytes) $(cat /sys/class/net/enp42s0/statistics/rx_bytes)"; sleep 1; done > $OUT/nic.log) &
until grep -q load_start $OUT/phases.txt 2>/dev/null; do sleep 1; kill -0 $SP || exit 1; done
$BL "\$a=Get-NetAdapterStatistics -Name Wi-Fi; \"before \$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) \$(\$a.ReceivedBytes) \$(\$a.SentBytes)\"" > $OUT/beelink_nic.txt 2>/dev/null
$BL "\$a=New-ScheduledTaskAction -Execute 'C:\Program Files\Moonlight Game Streaming\Moonlight.exe' -Argument 'stream 192.168.1.218 Desktop $ML_ARGS'; \$p=New-ScheduledTaskPrincipal -UserId jonah -LogonType Interactive; Register-ScheduledTask -TaskName mlstream -Action \$a -Principal \$p -Force | Out-Null; Start-ScheduledTask -TaskName mlstream; 'ok'" > $OUT/ml_start.txt 2>&1
echo "ml_start=$(date +%s.%N)" >> $OUT/phases.txt
wait $SP
$BL "\$a=Get-NetAdapterStatistics -Name Wi-Fi; \"after \$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) \$(\$a.ReceivedBytes) \$(\$a.SentBytes)\"" >> $OUT/beelink_nic.txt 2>/dev/null
$BL "Stop-Process -Name Moonlight -Force; Start-Sleep 3; \$f=Get-ChildItem \$env:TEMP\Moonlight*.log | Sort LastWriteTime | Select -Last 1; Select-String -Path \$f.FullName -Pattern FFmpeg,VerticalBlank -NotMatch | Select -Expand Line | Out-String -Width 250" > $OUT/moonlight.log 2>/dev/null
echo STREAMTEST_DONE

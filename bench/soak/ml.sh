#!/bin/bash
# usage: ml.sh "ARGS" -> run Moonlight.exe ARGS on the Beelink's desktop (a scheduled task, since ssh has
# no desktop session). Kills any running Moonlight first. E.g. "quit 192.168.1.218", "stream 192.168.1.218 Desktop".
exec ~/soak/bl.sh "Stop-Process -Name Moonlight -Force -ErrorAction SilentlyContinue; \$a=New-ScheduledTaskAction -Execute 'C:\Program Files\Moonlight Game Streaming\Moonlight.exe' -Argument '$1'; \$p=New-ScheduledTaskPrincipal -UserId jonah -LogonType Interactive; Register-ScheduledTask -TaskName ml -Action \$a -Principal \$p -Force | Out-Null; Start-ScheduledTask -TaskName ml; 'ok'"

#!/bin/bash
# usage: layout4k.sh -> start a Moonlight stream from the Beelink (creates the 4K virtual screen), launch
# PartyDeck with 2 MCC instances on it, then print where KWin put the gamescope windows. Leaves both running.
pkill -x repartydeck; ~/soak/mcc/cleanup.sh
~/soak/ml.sh "quit 192.168.1.218" >/dev/null; sleep 12; ~/soak/ml.sh "stream 192.168.1.218 Desktop" >/dev/null
for i in $(seq 60); do kscreen-doctor -o 2>/dev/null | grep -q Virtual-sunshine && break; sleep 1; done
P=$(cat /tmp/pads.out); IFS=, read -r a b c d e f g h <<< "$P"
(cd /tmp; systemd-run --user --scope --unit=mccl-$(date +%s) -q env PATH=$HOME/soak/mcc/bin:$PATH GS_VISIBLE=1 PARTYDECK_NETNS_PREFIX=pdi PARTYDECK_ALLOW_VIRTUAL_PADS=1 ~/Games/repartydeck-src/repartydeck/target/release/repartydeck --launch HaloMCC "$a+$b+$c+$d,$e+$f+$g+$h" --monitor 2 > ~/soak/mcc/runlayout.log 2>&1 &)
sleep 30; until [ "$(ps -eo etimes,args | awk '/gamescope -W/ && !/awk/ {print $1}' | sort -n | head -1)" -ge 115 ] 2>/dev/null; do sleep 5; done
cat > /tmp/wl.js <<'EOF'
for (const w of workspace.windowList()) { if (w.resourceClass.toString().indexOf("gamescope") >= 0) print("GSWIN " + JSON.stringify(w.frameGeometry) + " screen=" + w.output.name); }
EOF
id=$(qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript /tmp/wl.js wl-probe); qdbus6 org.kde.KWin /Scripting/Script$id org.kde.kwin.Script.run; sleep 1; qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript wl-probe >/dev/null
journalctl --user --since '-20s' --no-pager | grep -o 'GSWIN.*'
/tmp/sh2.sh /tmp/layout.png; echo LAYOUT_DONE

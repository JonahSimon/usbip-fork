# WiFi soak scripts

Copied from `~/soak` 2026-10-27. `soak.py` runs N emulated pads (ufk on the Beelink) with tcpdump, ping and link logs; `analyze.py` reads the result dir; `soak4k.sh` adds a live 4K Moonlight stream. `bl.sh` runs PowerShell on the Beelink over ssh (host alias `beelink`). IPs are this LAN (Beelink .200, PC .218).

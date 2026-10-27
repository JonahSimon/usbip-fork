#!/bin/bash
# run one PowerShell command on the Beelink
enc=$(printf '%s' "$1" | iconv -t UTF-16LE | base64 -w0)
exec ssh -o LogLevel=ERROR beelink powershell -NoProfile -EncodedCommand "$enc"

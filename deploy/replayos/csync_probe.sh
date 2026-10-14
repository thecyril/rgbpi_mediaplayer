#!/bin/sh
# Poll CH7101 page0 reg0x61 (0xff=locked, 0xef=dropout), log transitions only
i2cset -y -a 20 0x78 0x00 0x00
prev=""
while true; do
  v=$(i2cget -y -a 20 0x78 0x61 2>/dev/null || echo ERR)
  if [ "$v" != "$prev" ]; then echo "$(date +%H:%M:%S.%3N) 0x61=$v"; prev="$v"; fi
  sleep 0.1
done

#!/bin/sh
# Dump CH7101 registers pages 0-5, regs 0x01-0xFF (0x00 = page select, write-only)
BUS=20; ADDR=0x78
for page in 0 1 2 3 4 5; do
  i2cset -y -a $BUS $ADDR 0x00 $page 2>/dev/null
  echo "=== PAGE $page ==="
  for hi in 0 1 2 3 4 5 6 7 8 9 a b c d e f; do
    line=""
    for lo in 0 1 2 3 4 5 6 7 8 9 a b c d e f; do
      reg="0x${hi}${lo}"
      [ "$reg" = "0x00" ] && { line="$line --"; continue; }
      v=$(i2cget -y -a $BUS $ADDR $reg 2>/dev/null) || v=XX
      line="$line ${v#0x}"
    done
    echo "${hi}0:$line"
  done
done
i2cset -y -a $BUS $ADDR 0x00 0x00 2>/dev/null

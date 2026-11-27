#!/bin/bash
# Cut the reference body's outbound traffic to port 443 for a fixed number of
# seconds, keep SSH up, and always put it back (P12, D3).
#
# Installed root-owned at /usr/local/sbin/emet-netcut, behind one NOPASSWD
# sudo rule for this script alone (tools/body/README.md, "The network cut").
# tools/body/run.py --netcut SECONDS runs it through `sudo -n`, detached,
# with its output in a file, since a cut that took the SSH session with it
# lost the 0.4 console (RELEASE_PROCESS.md section 10, the 0.4 row).
#
#     emet-netcut SECONDS      a whole number from 5 to 600; nothing else
#
# What it does, with nftables, which Raspberry Pi OS ships (nftables 1.1.3 in
# the 2026-10-06 trixie image's package list, which has no iptables package;
# verified 2026-11-26 on downloads.raspberrypi.com):
#
# - one table of its own, `inet emet_netcut`, so no other rule is touched;
# - a set of ports whose one element, 443, has a timeout of SECONDS plus 5,
#   and one rule each for TCP and UDP that drops output to a port in the set.
#   The kernel deletes the element when its timeout runs out (the nftables
#   wiki, "Element timeouts", GFDL 1.3 or later; an idea, CITATIONS.md), so a
#   script killed outright leaves a rule that matches nothing: the body
#   cannot stay cut;
# - inbound SSH on port 22, and its replies, match no rule;
# - on its way out, by the end of the wait or by a signal, a trap deletes the
#   table.
#
# It prints what it did, a line starting `cut:` and a line starting
# `restored:`, which run.py reads.

set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 022

readonly TABLE=emet_netcut
readonly MIN_S=5
readonly MAX_S=600
readonly PORT=443
readonly SPARE_S=5

usage() {
  echo "usage: emet-netcut SECONDS, a whole number from $MIN_S to $MAX_S" >&2
  exit 2
}

# The one argument, checked before anything else runs.
[ "$#" -eq 1 ] || usage
case "$1" in
  '' | *[!0-9]*) usage ;;
esac
[ "${#1}" -le 3 ] || usage
seconds=$((10#$1))
[ "$seconds" -ge "$MIN_S" ] && [ "$seconds" -le "$MAX_S" ] || usage

if [ "$(id -u)" -ne 0 ]; then
  echo "emet-netcut: run it as root, through sudo -n" >&2
  exit 3
fi
if ! command -v nft >/dev/null 2>&1; then
  echo "emet-netcut: nft is not installed, so nothing was cut" >&2
  exit 4
fi

pause=""
restore() {
  if [ -n "$pause" ]; then kill "$pause" 2>/dev/null || true; fi
  if nft delete table inet "$TABLE" 2>/dev/null; then
    echo "restored: table inet $TABLE deleted at $(date -Iseconds)"
  else
    echo "restored: no table inet $TABLE was left at $(date -Iseconds)"
  fi
}
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# A table left by a run that was killed outright is replaced whole.
nft delete table inet "$TABLE" 2>/dev/null || true
nft add table inet "$TABLE"
nft add set inet "$TABLE" cut '{ type inet_service; flags timeout; }'
nft add chain inet "$TABLE" output '{ type filter hook output priority 0; policy accept; }'
nft add rule inet "$TABLE" output tcp dport @cut drop
nft add rule inet "$TABLE" output udp dport @cut drop
# The element goes in last: the cut starts here, and ends by itself.
nft add element inet "$TABLE" cut "{ $PORT timeout $((seconds + SPARE_S))s }"
echo "cut: outbound TCP and UDP to port $PORT dropped from $(date -Iseconds) for $seconds s; the rule lapses by itself after $((seconds + SPARE_S)) s"

# A wait a signal can end: bash runs a trap only between commands, and
# `wait` returns at once when a trapped signal arrives.
sleep "$seconds" &
pause=$!
wait "$pause"

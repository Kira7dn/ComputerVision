#!/bin/sh
set -eu

interface=$(ip route show default | awk 'NR == 1 { print $5 }')
address=$(ip -4 -o addr show dev "$interface" scope global | awk 'NR == 1 { split($4, value, "/"); print value[1] }')
test -n "$address"
exec /usr/bin/avahi-publish-address -R vision.local "$address"

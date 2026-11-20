#!/bin/sh
# Installs or removes the S1 spike units. Run with sudo; OWNER is the desktop user.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
owner=${SUDO_USER:?run with sudo from the desktop user}

case "${1:-install}" in
install)
    sed "s|@OWNER@|$owner|" "$here/tokenbooth-spike-booth.socket" > /etc/systemd/system/tokenbooth-spike-booth.socket
    sed "s|@REPO@|$repo|" "$here/tokenbooth-spike-booth@.service" > /etc/systemd/system/tokenbooth-spike-booth@.service
    systemctl daemon-reload
    systemctl start tokenbooth-spike-booth.socket
    ;;
remove)
    systemctl stop tokenbooth-spike-booth.socket || true
    rm -f /etc/systemd/system/tokenbooth-spike-booth.socket /etc/systemd/system/tokenbooth-spike-booth@.service
    systemctl daemon-reload
    ;;
esac

#!/bin/sh
# Installs or removes the booth units, serving the code in this checkout read-only.
# Run with sudo; the socket belongs to the user running sudo.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
owner=${SUDO_USER:?run with sudo from the desktop user}

case "${1:-install}" in
install)
    sed "s|@OWNER@|$owner|" "$here/tokenbooth-booth.socket" > /etc/systemd/system/tokenbooth-booth.socket
    sed "s|@REPO@|$repo|" "$here/tokenbooth-booth@.service" > /etc/systemd/system/tokenbooth-booth@.service
    systemctl daemon-reload
    systemctl enable --now tokenbooth-booth.socket
    ;;
remove)
    systemctl disable --now tokenbooth-booth.socket || true
    rm -f /etc/systemd/system/tokenbooth-booth.socket /etc/systemd/system/tokenbooth-booth@.service
    systemctl daemon-reload
    ;;
*)
    echo "usage: $0 [install|remove]" >&2
    exit 2
    ;;
esac

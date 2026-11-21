#!/bin/sh
# Installs or removes the S5 spike units. Run with sudo; OWNER is the desktop user.
# Chrome's sandbox needs nothing of ours: Ubuntu's /etc/apparmor.d/chrome grants it userns.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
owner=${SUDO_USER:?run with sudo from the desktop user}

case "${1:-install}" in
install)
    sed "s|@OWNER@|$owner|" "$here/tokenbooth-spike-chrome.socket" > /etc/systemd/system/tokenbooth-spike-chrome.socket
    sed "s|@REPO@|$repo|" "$here/tokenbooth-spike-chrome@.service" > /etc/systemd/system/tokenbooth-spike-chrome@.service
    systemctl daemon-reload
    systemctl start tokenbooth-spike-chrome.socket
    ;;
remove)
    systemctl stop tokenbooth-spike-chrome.socket || true
    rm -f /etc/systemd/system/tokenbooth-spike-chrome.socket /etc/systemd/system/tokenbooth-spike-chrome@.service
    systemctl daemon-reload
    ;;
esac

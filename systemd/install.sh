#!/bin/sh
# Installs or removes the daemon and the booth units. Run with sudo, after
# `cargo build --release` as yourself. The daemon and the booth's code are copied root-owned, to
# /usr/local/bin and /usr/local/lib/tokenbooth: the booth is handed decrypted keep-sets, so its
# code must be out of reach of the owner's processes. Only the daemon may start booths.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
binary="$repo/target/release/tokenbooth"
lib=/usr/local/lib/tokenbooth

case "${1:-install}" in
install)
    [ -x "$binary" ] || { echo "no $binary: run cargo build --release first" >&2; exit 1; }
    install -D -m 0644 "$here/tokenbooth.sysusers" /etc/sysusers.d/tokenbooth.conf
    systemd-sysusers /etc/sysusers.d/tokenbooth.conf
    install -m 0755 "$binary" /usr/local/bin/tokenbooth
    rm -rf "$lib"
    install -d -m 0755 "$lib/tokenbooth/engines"
    install -m 0644 "$repo"/tokenbooth/*.py "$lib/tokenbooth/"
    install -m 0644 "$repo"/tokenbooth/engines/*.py "$lib/tokenbooth/engines/"
    python3 -I -m compileall -q "$lib"
    install -m 0644 "$here/tokenbooth.socket" "$here/tokenbooth.service" /etc/systemd/system/
    install -m 0644 "$here/tokenbooth-booth.socket" /etc/systemd/system/
    install -m 0644 "$here/tokenbooth-booth@.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable tokenbooth-booth.socket tokenbooth.socket
    systemctl restart tokenbooth-booth.socket tokenbooth.socket
    systemctl try-restart tokenbooth.service
    ;;
remove)
    systemctl disable --now tokenbooth.socket tokenbooth.service tokenbooth-booth.socket || true
    rm -f /etc/systemd/system/tokenbooth.socket /etc/systemd/system/tokenbooth.service \
        /etc/systemd/system/tokenbooth-booth.socket /etc/systemd/system/tokenbooth-booth@.service \
        /usr/local/bin/tokenbooth /etc/sysusers.d/tokenbooth.conf
    rm -rf "$lib"
    systemctl daemon-reload
    echo "kept: the tokenbooth user and /var/lib/tokenbooth" >&2
    ;;
*)
    echo "usage: $0 [install|remove]" >&2
    exit 2
    ;;
esac

#!/bin/sh
# Gives the S1 booth an interpreter of its own and an AppArmor profile allowing it user
# namespaces. Run with sudo after spikes/s1-booth-unit/install.sh.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
prefix=/opt/tokenbooth-spike-bin
python=/usr/bin/python3.14
version=$($python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')

case "${1:-install}" in
install)
    install -d -m 0755 "$prefix/bin" "$prefix/lib/python$version"
    # a copy, not a link: AppArmor attaches by the resolved path
    install -m 0755 "$python" "$prefix/bin/booth"
    printf 'home = /usr/bin\ninclude-system-site-packages = false\nversion = %s\n' \
        "$($python -c 'import platform; print(platform.python_version())')" > "$prefix/pyvenv.cfg"
    # resolved inside the booth unit, where the repo is bound at /opt/tokenbooth-spike
    ln -sfn "/opt/tokenbooth-spike/.venv/lib/python$version/site-packages" "$prefix/lib/python$version/site-packages"
    install -m 0644 "$here/tokenbooth-spike-booth.apparmor" /etc/apparmor.d/tokenbooth-spike-booth
    apparmor_parser -r /etc/apparmor.d/tokenbooth-spike-booth
    install -d -m 0755 /etc/systemd/system/tokenbooth-spike-booth@.service.d
    install -m 0644 "$here/s2.conf" /etc/systemd/system/tokenbooth-spike-booth@.service.d/s2.conf
    systemctl daemon-reload
    ;;
remove)
    rm -f /etc/systemd/system/tokenbooth-spike-booth@.service.d/s2.conf
    rmdir /etc/systemd/system/tokenbooth-spike-booth@.service.d 2>/dev/null || true
    systemctl daemon-reload
    if [ -e /etc/apparmor.d/tokenbooth-spike-booth ]; then
        apparmor_parser -R /etc/apparmor.d/tokenbooth-spike-booth || true
        rm -f /etc/apparmor.d/tokenbooth-spike-booth
    fi
    rm -rf "$prefix"
    ;;
esac

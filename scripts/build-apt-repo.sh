#!/bin/bash
# Builds a signed apt repository from the .deb files in DEBS into OUT: suite and codename
# "stable", component "main", amd64. Signs with KEY, which must be in gpg's keyring. A
# package's provenance bundle, DEB.sigstore.json in DEBS, is published beside it.
# Usage: scripts/build-apt-repo.sh DEBS OUT KEY
set -euo pipefail

debs=$(realpath "$1")
out=$2
key=$3

rm -rf "$out"
mkdir -p "$out/pool/main" "$out/dists/stable/main/binary-amd64"
cp "$debs"/*.deb "$out/pool/main/"
find "$debs" -maxdepth 1 -name '*.deb.sigstore.json' -exec cp -t "$out/pool/main/" {} +
cd "$out"

apt-ftparchive packages pool > dists/stable/main/binary-amd64/Packages
gzip -9 -k -n dists/stable/main/binary-amd64/Packages
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=tokenbooth \
    -o APT::FTPArchive::Release::Label=tokenbooth \
    -o APT::FTPArchive::Release::Suite=stable \
    -o APT::FTPArchive::Release::Codename=stable \
    -o APT::FTPArchive::Release::Architectures=amd64 \
    -o APT::FTPArchive::Release::Components=main \
    release dists/stable > dists/stable/Release

gpg --batch --yes --local-user "$key" --clearsign --output dists/stable/InRelease dists/stable/Release
gpg --batch --yes --local-user "$key" --armor --detach-sign --output dists/stable/Release.gpg dists/stable/Release
gpg --armor --export "$key" > tokenbooth.asc
fingerprint=$(gpg --with-colons --fingerprint "$key" | awk -F: '$1 == "fpr" { print $10; exit }')

cat > index.html <<HTML
<!doctype html>
<meta charset="utf-8">
<title>tokenbooth apt repository</title>
<h1>tokenbooth apt repository</h1>
<p>For Ubuntu 26.04 or later, amd64. It needs pidvault and Google Chrome from their own repositories. See <a href="https://github.com/torstei/tokenbooth">the project</a>.</p>
<pre>
sudo curl -fsSLo /etc/apt/keyrings/tokenbooth.asc https://torstei.github.io/tokenbooth/tokenbooth.asc
echo "deb [signed-by=/etc/apt/keyrings/tokenbooth.asc] https://torstei.github.io/tokenbooth stable main" \\
  | sudo tee /etc/apt/sources.list.d/tokenbooth.list
sudo apt update &amp;&amp; sudo apt install tokenbooth
</pre>
<p>Signing key fingerprint: <code>$fingerprint</code></p>
HTML

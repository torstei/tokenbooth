# Changelog

## 0.1.0 - 2026-11-26

First release, for Ubuntu 26.04 or later on amd64.

- `tokenbooth get|login NAME` hands a site's token to a shell that pidvault has unlocked
  the profile's vault in: from the daemon's cache while it is valid, else from a login
  window. `--to-vault` writes it to `tokens/NAME` in the vault instead of printing it.
- The window is a booth: Google Chrome as a fresh dynamic user that owns nothing, in its
  own PID namespace and with no access to home directories, drawing through the caller's
  Wayland connection and allowed to navigate only to the profile's hosts.
- Cookies a profile names in `keep_cookies` are kept between logins, sealed with the
  token under a per-profile key kept in the vault.
- `tokenbooth profile list|show|set|delete`, `tokenbooth forget` and `tokenbooth status`.
- The package installs a policy for apt-attest, so with apt-attest installed apt accepts
  only packages that this repository's release workflow built and attested.

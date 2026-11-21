# tokenbooth

Log in to a site in a locked-down browser window and print the token it leaves behind.
`tokenbooth-dev` does it as you, from this checkout; `tokenbooth`, with its daemon, is what it is
becoming.
Prototype — see [DESIGN.md](DESIGN.md) for what it is meant to become.

## Setup

The booth drives Google Chrome (`google-chrome-stable` from Google's apt repository, at
`/opt/google/chrome/chrome`). `--engine qt` uses Qt WebEngine from the virtualenv instead.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[qt,test]'
.venv/bin/pytest                  # unit tests, and both engines offscreen (Chrome headless)
```

## Use

```sh
# ad hoc: open the login page, print the cookie once it appears
.venv/bin/tokenbooth-dev get gopro --url https://gopro.com/login --cookie gp_access_token

# or with a profile in <home>/profiles/gopro.toml (see examples/)
.venv/bin/tokenbooth-dev get gopro            # cached token if still valid, else the window
.venv/bin/tokenbooth-dev login gopro          # always the window
.venv/bin/tokenbooth-dev forget gopro         # drop the cached token and kept cookies
.venv/bin/tokenbooth-dev keep-set gopro       # list the kept cookies (never their values)
```

Each run starts with an empty browser.
Kept cookies are stored in the engine's own form, so after switching engines the first login is
a full one. `keep_cookies` in the profile, or `--keep NAME@DOMAIN`,
names cookies to carry into the next run, such as those that spare you 2-step verification.

For a new site, let tokenbooth find the cookie: sign in by hand, close the window (with
`--engine qt`, press "I'm logged in"), and it prints a profile using the cookie that appeared
with the login. Ctrl-C gives up without one.

```sh
.venv/bin/tokenbooth-dev discover github --url https://github.com/login --via google > github.toml
```

`-v` logs navigations (without query strings) and cookie names (without values) to stderr —
the way to find out which hosts a new site needs: a navigation it blocks shows as `BLOCKED`.

State lives in `--home`, `$TOKENBOOTH_HOME` or `~/.local/share/tokenbooth`; point it into a pidvault
vault to keep the kept cookies and tokens out of reach of everything but unlocked shells.

The window opens only on Wayland; on X11, where any client can read what is typed, it refuses.

### The daemon, and the booth as a user of its own

`tokenbooth` (Rust, in this repository's root) is the client and, as `tokenbooth daemon`, the
daemon that holds profiles, kept cookies and tokens for shells pidvault has unlocked
([DESIGN.md](DESIGN.md#steps), step 3). Every request needs the profile's vault unlocked in the
calling shell.

`systemd/install.sh` (with sudo) installs the daemon as the `tokenbooth` system user, copied
root-owned to `/usr/local/bin/tokenbooth`, and a socket-activated unit that runs each booth as
a fresh dynamic user with no access to your home directory, serving the booth's code in this
checkout read-only. Only the daemon may start booths.

```sh
cargo build --release
sudo systemd/install.sh                       # sudo systemd/install.sh remove  to take it out
pidvault unlock                               # the vault the profile names; `default` unless set
tokenbooth profile set gopro examples/gopro.toml
tokenbooth get gopro                          # cached token if still valid, else the window
tokenbooth login gopro -v                     # always the window, logging navigations
tokenbooth forget gopro                       # drop the cached token and kept cookies
tokenbooth get gopro --to-vault               # write it to /run/pidvault/stored/<vault>/tokens/gopro
```

A token on stdout passes through a pipe, which any of your processes can open while it is in
flight. `--to-vault` puts it in the profile's vault instead, readable only by processes the
vault is unlocked for; that is the place for a program to read it from.

The daemon keeps each profile's token and kept cookies sealed with a key it never stores: the
key is in your vault, at `/run/pidvault/stored/<vault>/tokenbooth/<profile>.key`, made on first
use. Your processes cannot read the sealed state, the daemon's; a copy of the disk cannot open
it without the vault. Lose the key and the next login starts afresh.

The booth unit runs Google Chrome only. The logs are in `journalctl -u tokenbooth -u 'tokenbooth-booth@*'`.
`tokenbooth` exits 0 with a token printed, 1 when the window was closed without one, 2 on an
error or refusal, 3 on a timeout.

The `tokenbooth-dev` exit status: 0 token printed, 1 window closed without one, 2 usage error
or refused, 3 timed out.

## License

Licensed under the Apache License, Version 2.0 ([LICENSE](LICENSE)).

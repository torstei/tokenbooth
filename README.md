# tokenbooth

Log in to a site in a locked-down browser window and print the token it leaves behind.
Prototype — see [DESIGN.md](DESIGN.md) for what it is meant to become.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest                  # unit tests, and the cookie store offscreen
```

## Use

```sh
# ad hoc: open the login page, print the cookie once it appears
.venv/bin/tokenbooth get gopro --url https://gopro.com/login --cookie gp_access_token

# or with a profile in <home>/profiles/gopro.toml (see examples/)
.venv/bin/tokenbooth get gopro            # cached token if still valid, else the window
.venv/bin/tokenbooth login gopro          # always the window
.venv/bin/tokenbooth forget gopro         # drop the cached token and kept cookies
.venv/bin/tokenbooth keep-set gopro       # list the kept cookies (never their values)
```

Each run starts with an empty browser. `keep_cookies` in the profile, or `--keep NAME@DOMAIN`,
names cookies to carry into the next run, such as those that spare you 2-step verification.

For a new site, let tokenbooth find the cookie: sign in by hand, press "I'm logged in",
and it prints a profile using the cookie that appeared with the login.

```sh
.venv/bin/tokenbooth discover github --url https://github.com/login --via google > github.toml
```

`-v` logs navigations (without query strings) and cookie names (without values) to stderr —
the way to find out which hosts a new site needs: a navigation it blocks shows as `BLOCKED`.

State lives in `--home`, `$TOKENBOOTH_HOME` or `~/.local/share/tokenbooth`; point it into a pidvault
vault to keep the kept cookies and tokens out of reach of everything but unlocked shells.

The window opens only on Wayland; on X11, where any client can read what is typed, it refuses.

Exit status: 0 token printed, 1 window closed without one, 2 usage error or refused, 3 timed out.

## License

Licensed under the Apache License, Version 2.0 ([LICENSE](LICENSE)).

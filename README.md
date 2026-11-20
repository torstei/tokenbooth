# tokenbooth

Log in to a site in a locked-down browser window and print the token it leaves behind.
Prototype — see [DESIGN.md](DESIGN.md) for what it is meant to become.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Use

```sh
# ad hoc: open the login page, print the cookie once it appears
.venv/bin/tokenbooth get gopro --url https://gopro.com/login --cookie gp_access_token

# or with a profile in <home>/profiles/gopro.toml (see examples/)
.venv/bin/tokenbooth get gopro            # cached token if still valid, else the window
.venv/bin/tokenbooth login gopro          # always the window
.venv/bin/tokenbooth forget gopro         # drop the cached token and saved session
```

`-v` logs navigations (without query strings) and cookie names (without values) to stderr —
the way to find out which hosts and cookie a new site needs.

State lives in `--home`, `$TOKENBOOTH_HOME` or `~/.local/share/tokenbooth`; point it into a pidvault
vault to keep sessions and tokens out of reach of everything but unlocked shells.

Exit status: 0 token printed, 1 window closed without one, 2 usage error, 3 timed out.

## License

Licensed under the Apache License, Version 2.0 ([LICENSE](LICENSE)).

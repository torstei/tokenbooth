"""tokenbooth-dev: log in to a site in a locked-down browser window and print the token it leaves behind.

Exit status: 0 token printed, 1 window closed without a token, 2 usage error, 3 timed out.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from tokenbooth import protocol, store
from tokenbooth.engines import ENGINES
from tokenbooth.discover import Recorder, describe, site_of, suggest_profile, tied

EXIT_OK, EXIT_CANCELLED, EXIT_USAGE, EXIT_TIMEOUT = 0, 1, 2, 3


def profile_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("name", help="profile name; reads <home>/profiles/NAME.toml if it exists")
    p.add_argument("--url", dest="start_url", help="login page to open")
    p.add_argument("--cookie", help="name of the cookie that holds the token")
    p.add_argument("--cookie-domain", help="domain the cookie must belong to (default: the start URL's)")
    p.add_argument("--allow", dest="extra_hosts", action="append", default=[], metavar="HOST",
                   help="also allow navigation to HOST (glob, repeatable), on top of the profile's allowed_hosts"
                        " or, without one, the start host and its subdomains")
    p.add_argument("--via", action="append", default=[], metavar="PROVIDER",
                   help="sign in through an identity provider: allow its hosts and adapt to it (known: google, microsoft)")
    p.add_argument("--done-url", help="regex the page URL must match before the cookie counts")
    p.add_argument("--keep", dest="keep_cookies", action="append", metavar="NAME@DOMAIN",
                   help="keep matching cookies for the next run (globs, repeatable); replaces the profile's keep_cookies")
    p.add_argument("--user-agent", help="replace the user agent string")
    p.add_argument("--timeout", type=int, help="seconds before giving up (default 600, 0 = never)")
    p.add_argument("--json", action="store_true", help='print {"token": ..., "expires": ...} instead of the bare token')


def common_options(p: argparse.ArgumentParser, suppress: bool) -> None:
    p.add_argument("--home", type=Path, default=argparse.SUPPRESS if suppress else None,
                   help="state directory (default: $TOKENBOOTH_HOME or ~/.local/share/tokenbooth)")
    p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS if suppress else False,
                   help="log navigations and cookie names (never values) to stderr")
    p.add_argument("--engine", choices=ENGINES, default=argparse.SUPPRESS if suppress else "chrome",
                   help="the browser the booth drives: Google Chrome (default) or Qt WebEngine;"
                        " ignored with --booth-socket, where the unit decides")
    p.add_argument("--booth-socket", metavar="PATH",
                   default=argparse.SUPPRESS if suppress else os.environ.get("TOKENBOOTH_BOOTH_SOCKET"),
                   help="ask the booth unit listening at PATH, which runs as a user of its own, instead of"
                        " starting a booth as you (default: $TOKENBOOTH_BOOTH_SOCKET)")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tokenbooth-dev", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    common_options(parser, suppress=False)
    # SUPPRESS keeps a subcommand that was not given the option from resetting the top-level value
    common = argparse.ArgumentParser(add_help=False)
    common_options(common, suppress=True)
    sub = parser.add_subparsers(dest="command", required=True)
    profile_options(sub.add_parser("get", parents=[common], help="print a cached token, or open the login window to get one"))
    profile_options(sub.add_parser("login", parents=[common], help="always open the login window"))
    profile_options(sub.add_parser(
        "discover", parents=[common],
        help="sign in by hand, then close the window (Qt: press \"I'm logged in\"): lists the cookies"
             " that appeared with the login and prints a profile using the likeliest one"))
    forget = sub.add_parser("forget", parents=[common], help="delete the cached token and the kept cookies")
    forget.add_argument("name")
    keep_set = sub.add_parser("keep-set", parents=[common], help="list the kept cookies (names, domains and expiry, never values)")
    keep_set.add_argument("name")
    return parser.parse_args(argv)


def emit(token: str, expires: float | None, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"token": token, "expires": expires}))
    else:
        print(token)


def report_discovery(profile: store.Profile, recorder: Recorder) -> int:
    site = site_of(urlsplit(profile.start_url).hostname or "")
    candidates = recorder.candidates(site)
    print(f"tokenbooth: cookies on {site}, in the order they appeared:", file=sys.stderr)
    for seen in recorder.site_cookies(site):
        if seen in candidates:
            mark = "*" if seen is candidates[0] else "+"
        else:
            mark = "-" if seen.before else " "
        print(f"  {mark} {describe(seen)}", file=sys.stderr)
    print("tokenbooth: * likeliest  + also new with the login  - set before the login started"
          "  (blank) its name was set before, or it is gone again", file=sys.stderr)
    if not candidates:
        print("tokenbooth: no cookie appeared with the login; did the sign-in finish before the window closed?",
              file=sys.stderr)
        return EXIT_CANCELLED
    if len(even := tied(candidates)) > 1:
        print("tokenbooth: these rank the same, so the first to appear was chosen; check which one the"
              " site's requests need: " + ", ".join(f"{s.name}@{s.domain}" for s in even), file=sys.stderr)
    print(suggest_profile(profile, candidates[0]), end="")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    home = args.home or store.default_home()
    try:
        if args.command == "forget":
            for path in store.forget(home, args.name):
                print(f"removed {path}", file=sys.stderr)
            return EXIT_OK
        if args.command == "keep-set":
            for entry in store.load_keep(home, args.name):
                expires = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["expires"])) if entry["expires"] else "session"
                print(f"{entry['name']}@{entry['domain']}{entry['path']}\t{expires}")
            return EXIT_OK

        overrides = {k: getattr(args, k) for k in (
            "start_url", "cookie", "cookie_domain", "done_url",
            "keep_cookies", "user_agent", "timeout")}
        discovering = args.command == "discover"
        profile = store.Profile.load(home, args.name, overrides, args.extra_hosts, args.via,
                                     require_cookie=not discovering,
                                     engine="chrome" if args.booth_socket else args.engine)

        if args.command == "get" and (token := store.cached_token(home, profile.name)):
            emit(token, None, args.json)
            return EXIT_OK

        request = protocol.Request(mode="discover" if discovering else "login", profile=profile,
                                   keep=store.load_keep(home, profile.name), log=args.verbose)
        def on_log(text: str) -> None:
            print(f"tokenbooth: {text}", file=sys.stderr, flush=True)

        if args.booth_socket:
            with protocol.wayland_connection(dict(os.environ)) as wayland:
                result = protocol.connect_booth(args.booth_socket, request, wayland, on_log)
        else:
            result = protocol.run_booth(request, on_log, engine=args.engine)
    except store.UsageError as e:
        print(f"tokenbooth: {e}", file=sys.stderr)
        return EXIT_USAGE
    except protocol.ProtocolError as e:
        print(f"tokenbooth: {e}", file=sys.stderr)
        return EXIT_USAGE

    if result.outcome == "refused":
        print(f"tokenbooth: the booth refused: {result.error}", file=sys.stderr)
        return EXIT_USAGE
    if profile.keep_cookies:
        store.save_keep(home, profile.name, result.keep)
        if args.verbose:
            print(f"tokenbooth: kept {len(result.keep)} cookie(s): "
                  + " ".join(f"{e['name']}@{e['domain']}" for e in result.keep), file=sys.stderr)
    if discovering:
        if result.outcome != "confirmed":
            print("tokenbooth: timed out" if result.outcome == "timed_out" else
                  "tokenbooth: closed without confirming the login; nothing suggested", file=sys.stderr)
            return EXIT_TIMEOUT if result.outcome == "timed_out" else EXIT_CANCELLED
        return report_discovery(profile, Recorder.from_cookies(result.cookies or []))
    if result.outcome != "token":
        print("tokenbooth: timed out" if result.outcome == "timed_out" else "tokenbooth: closed without a token",
              file=sys.stderr)
        return EXIT_TIMEOUT if result.outcome == "timed_out" else EXIT_CANCELLED
    expires = store.save_token(home, profile.name, result.token, result.expires)
    emit(result.token, expires, args.json)
    return EXIT_OK

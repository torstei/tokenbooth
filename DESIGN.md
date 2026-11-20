# tokenbooth — a browser for one login

`tokenbooth` opens a dedicated, locked-down browser window at a site's login page,
waits for you to sign in, and hands the token the site leaves behind to whoever asked for it.
It replaces the dance of signing in with your everyday browser, opening developer tools and
copying a cookie out by hand.

## Goals

- **One job.** The window exists to complete one login. No URL bar, no tabs, no navigation
  outside the hosts the login needs, no downloads. You cannot drift into using it for browsing.
- **Unmistakable.** It looks nothing like your normal browser: a loud frame, its own title,
  its own Wayland app-id / X11 `WM_CLASS` and icon, so the window manager can tell it apart too.
- **Started from the command line**, opened at the login page, and done when the token appears.
- **The token goes to the caller**, on stdout — not to the clipboard, which every process can read.
- **Storage is a directory you choose.** A plain directory works on its own; pointing it into a
  [pidvault](https://github.com/torstei/pidvault) vault makes the session and tokens visible
  only to shells that unlocked it. The vault is the intended home.

## Non-goals (for now)

- How it is ultimately driven. The idea is an agent you ask "a login for `$FOO`, starting at
  `$URL`", which returns a token it already holds or opens the window to get one. That waits
  until the prototype shows the embedded browser can get through real logins at all.
- Being a general browser, or an automation tool that fills in credentials for you.

## First target

The GoPro cloud (used by `mushcam`): login at gopro.com leaves a `gp_access_token` cookie, which
is all the API needs. Today `mushcam gopro-login` asks you to paste it from developer tools.

## Shape

```
 caller (an unlocked shell, a script, later an agent)
   │  tokenbooth get gopro
   ▼
 ┌──────────── tokenbooth ─────────────────┐
 │ 1. load profile                     │
 │ 2. cached token still valid? ───────┼──► token on stdout, exit 0
 │ 3. otherwise open the window        │
 └───────────────┬─────────────────────┘
                 ▼
 ┌─ TOKEN BOOTH · gopro ─────────────────────────────┐  ◄ loud frame, own app-id
 │  🔒 gopro.com                         [Cancel]     │  ◄ read-only host + TLS strip
 │ ┌────────────────────────────────────────────────┐ │
 │ │           QWebEngineView (login page)          │ │
 │ └────────────────────────────────────────────────┘ │
 └────────────────────────────────────────────────────┘
        │ watches cookies and navigations
        ▼
   token appears        → window closes, token on stdout, exit 0
   window closed        → exit 1
   timeout              → exit 3
```

Python + PySide6 (QtWebEngine, i.e. Chromium) for the prototype. The pip wheels bundle a
current Qt, so client-hint control and WebAuthn are available. From 6.12, QtWebEngine is its own
wheel, `PySide6-WebEngine`, no longer part of `PySide6-Addons`. A packaged version could move to
C++ against distro Qt later; Ubuntu 24.04's Qt 6.4 is too old for some of what is used here.

## Home directory

Everything lives under one directory, `--home`, `$TOKENBOOTH_HOME`, or `~/.local/share/tokenbooth`:

```
<home>/profiles/<name>.toml   what to open and what to capture
<home>/sessions/<name>/       the browser profile, when persist_session = true
<home>/tokens/<name>.json     the cached token and its expiry (0600)
```

One directory, so moving it into a vault moves all of it — including the profiles, whose
`allowed_hosts` would otherwise be editable by any process running as you.

## Profiles

```toml
start_url       = "https://gopro.com/login"
allowed_hosts   = ["gopro.com", "*.gopro.com"]
cookie          = "gp_access_token"
cookie_domain   = "gopro.com"
done_url        = ""        # optional regex the page URL must match before the cookie counts
persist_session = false     # keep the logged-in browser profile between runs
user_agent      = ""        # override; default is the engine's own UA without "QtWebEngine"
timeout         = 600
```

Every key can also be given on the command line, so `tokenbooth get foo --url https://… --cookie x`
works with no profile file. Without `allowed_hosts`, the start URL's host and its subdomains
are allowed.

## Locking the window down

Embedding means nothing browser-like exists unless it is built:

- **Navigation allowlist.** `QWebEnginePage.acceptNavigationRequest` lets top-level navigation
  reach only `allowed_hosts`, over https. Subframes and subresources (CDNs, captchas) load freely.
- **Popups.** Some identity providers need a real popup that talks back to its opener, so
  `createWindow` opens another locked window of the same kind, on the same profile.
- **No context menu, downloads, fullscreen, clipboard access from script, plugins, or
  permissions** (camera, location, notifications are all refused).
- **The host strip** shows where you are and whether it is TLS. Without a URL bar it is the
  only way to see where you are typing a password.

## Extracting the token

| Mode | How | For |
|---|---|---|
| `cookie` | `QWebEngineCookieStore.cookieAdded`, which includes HttpOnly cookies | session cookies (GoPro) — **prototype** |
| `localstorage` | `runJavaScript` in the right origin once the login is done | SPAs keeping a JWT in storage (MSAL.js, Auth0) |
| `header` | a script injected at document creation wraps `fetch`/XHR and reports `Authorization: Bearer …` | APIs where the token only travels in requests |
| `oauth` | a real OAuth client with PKCE; the redirect to `http://127.0.0.1/cb` is caught in `acceptNavigationRequest` and the code exchanged directly | services that let you register a client |

## The user agent and embedded-browser blocks

The user agent is ours to set. QtWebEngine's default contains `QtWebEngine/x.y`; the prototype
removes it and otherwise reports the engine's real Chromium version. The rest of the problem:

- **Client hints.** `Sec-CH-UA` and `navigator.userAgentData` must agree with the string
  (`QWebEngineClientHints`, Qt 6.8+). With PySide6 6.12 (Chromium 140) they already report
  plain `Chromium` with no Qt brand, and `navigator.webdriver` is false, so after the UA fix the
  window presents as a stock Chromium. Claiming to be Google Chrome would mean adding that brand.
- **Engine version lag.** QtWebEngine trails Chrome; claiming a newer Chrome than the engine is
  can be detected from features.
- **Google** judges more than the string ("This browser or app may not be secure"). Other
  QtWebEngine browsers (qutebrowser, Falkon) get through by adjusting the user agent.
  Microsoft Entra allows embedded browsers; most other sites do not look.

## Security

- **Tokens go to stdout only.** The cache sits in `<home>`, 0600, in 0700 directories.
- **Off the record by default.** Without `persist_session` the browser profile lives in memory
  and is gone when the window closes.
- **No remote control.** It refuses to start when `QTWEBENGINE_REMOTE_DEBUGGING` is set, drops
  `QTWEBENGINE_CHROMIUM_FLAGS`, and passes Qt no command-line arguments; any of those could open
  a debugging port that gives every local process the logged-in session.
- **The Chromium sandbox stays on.**
- **Logs never contain token values.** `-v` logs navigations without query or fragment, and
  cookie names without values.
- **X11:** any program on the display can read what you type, as with pidvault. Wayland does not
  allow that.

## Passkeys and security keys

Qt 6.7 added WebAuthn (`QWebEngineWebAuthUxRequest`); the application draws the PIN and touch
prompts itself, and needs access to the key's `/dev/hidraw` device. USB keys should work;
signing in with a passkey on a phone probably does not. Not in the prototype.

## What the prototype has shown

1. **GoPro works**, signing in with Google: the embedded window completes the login and
   captures `gp_access_token`, a 7-day cookie, so `tokenbooth get` answers from the cache for a week.
2. **Google** refuses the embedded browser with a clean Chromium user agent ("This browser or
   app may not be secure") and accepts it with a Firefox one, the same trick qutebrowser uses.
   Phone-prompt 2-step verification works. After sign-in, Google bounces the top-level page
   through `accounts.youtube.com` and `accounts.google.<country>` (`SetSID`) to set cookies
   there. `--via google` / `via = ["google"]` bundles those hosts and the user agent.
   If Google stops accepting this, the fallback is a second engine: the real Chrome binary on
   its own profile, controlled over `--remote-debugging-pipe` (reachable only by tokenbooth), so
   that Google sees genuine Chrome.
3. **Host globs:** `*` matches exactly one DNS label and only a leading `*.` matches any depth,
   so `accounts.google.*` admits `accounts.google.no` but not `accounts.google.evil.com`.

Still open: Entra and GitHub; whether the saved session makes the next login a single click;
then the agent and how the commands should look.

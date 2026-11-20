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
- **The token goes to the caller**, never to the clipboard, which every process can read.
- **Protected from the owner's other processes**, as [pidvault](https://github.com/torstei/pidvault)
  protects SSH keys: the booth runs as a throwaway service user, and only shells that
  pidvault has unlocked can ask for a login. See [Security model](#security-model).
- **A defined, minimal state between runs.** Each booth starts empty. What survives is named
  explicitly — the token, and the cookies that spare you 2-step verification next time —
  never a whole browser profile.

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

## Home directory (prototype)

The prototype runs as the user and keeps everything under one directory, `--home`,
`$TOKENBOOTH_HOME`, or `~/.local/share/tokenbooth`:

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

## Security model

This is the design the prototype is moving towards; it follows pidvault's
[security design](https://github.com/torstei/pidvault/blob/main/docs/security-design.md) and
reuses its notion of an unlocked shell. [The prototype](#the-prototype) runs as the user and
makes none of the claims below.

### Assets

1. **Identity-provider sessions.** The cookies a login leaves at Google (or Entra, GitHub…)
   sign in as you to that whole account, not just to the site that asked. The most valuable
   asset, and the one a persisted browser profile quietly accumulates.
2. **Remembered-device state.** What lets the next login skip 2-step verification. It may be
   the same cookies as 1, or a subset; which, is a per-provider question to settle by experiment.
3. **The tokens** handed to callers, such as `gp_access_token`.
4. **The integrity of profiles.** A changed `start_url` or `allowed_hosts` sends your password
   to someone else's page, inside a window whose whole point is that you trust it.
5. **What is typed into the booth:** passwords and one-time codes.

### Adversaries

| | Adversary | Can |
|---|---|---|
| A1 | A process of the owner outside an unlocked shell: an AI agent, a browser, a `curl \| sh` script | Everything the owner's uid can: read and write the home directory and `$XDG_RUNTIME_DIR`, connect to the owner's sockets, open the owner's processes' pipes through `/proc/<pid>/fd`, run its own Wayland clients |
| A2 | Another local user | Connect to tokenbooth's socket as themselves |
| A3 | A hostile or compromised web page in the booth | Run script in the booth, and with a Chromium exploit, code as the booth's uid |
| A4 | Someone with a copy of the disk or a backup | Read anything stored unencrypted |
| A5 | The supply chain: PySide6/Qt/Chromium, the build, the package | Change what runs |

### Out of scope

- **Root and the kernel.**
- **What runs inside an unlocked shell.** It can get tokens — that is what the shell is for —
  and the key that opens the stored state, but not the state itself (see T2).
- **The compositor and the code it loads**; see [Known limitations](#known-limitations).
- **Physical access to a running machine.**

### Components

```
 unlocked shell (pidvault)                          tokenboothd (uid tokenbooth, sandboxed)
   tokenbooth get gopro ───── /run/tokenbooth/sock ─────► SO_PEERCRED + SO_PEERPIDFD:
     │  + a Wayland connection fd (SCM_RIGHTS)              caller's uid, and is its shell unlocked?
     │  + the state key, read from the vault                │
     │                                                       │ starts, with a clean environment
     │                                                       ▼
     │                                         booth (fresh DynamicUser uid, in-memory profile)
     │                                           WAYLAND_SOCKET = the passed fd
     │                                           seeded with the decrypted keep-set, exits after
     │                                           one login, returns token + updated keep-set
     ◄── token, over the socket ──────────────────────────┘
                                                stored: /var/lib/tokenbooth/<uid>/<profile>
                                                (keep-set encrypted with the key from the vault)
```

- **The daemon** is small and long-lived (Rust, like pidvault). It owns the stored state and
  profiles, authenticates callers by the kernel's credentials and a pidfd, and asks whether
  the caller's shell is unlocked — through pidvault, or by sharing its session logic; that
  interface is the main integration question. The client trusts the daemon only if it runs
  as another uid behind a root-owned socket directory, as pidvault's client does.
- **The booth** is QtWebEngine — a browser, the largest attack surface by far — so it gets
  nothing it can keep. Each run is a new systemd unit with `DynamicUser=yes`: a uid that has
  never existed before and owns nothing, no `/home`, a private `/tmp`, an off-the-record
  browser profile held in memory, and a clean environment, so no `LD_PRELOAD`, `QT_*`,
  `PYTHON*` or `QTWEBENGINE_*` reaches it from the caller. It exits when the login completes
  or is cancelled, and everything it wrote goes with its uid.
- **The code is root-owned** under `/usr`, from a package, never a virtualenv in a home
  directory that A1 can rewrite.

### State that survives a run

Only this, per (owner, profile):

- **The token** and its expiry, so `get` can answer without a booth.
- **The keep-set:** cookies named per provider and profile (`keep_cookies`, e.g. the Google
  cookies that remember the device), harvested from the booth when it finishes and loaded
  into the next one before its first navigation (`QWebEngineCookieStore.setCookie`).
  Nothing else: no history, cache, local storage, service workers or permissions.

A profile chooses its level: **nothing** (2-step verification every time), **remember the
device** (type the password, skip the second factor, if the provider allows splitting it),
or **stay signed in** (one click, and the full provider session is stored).

The keep-set is stored by the daemon, encrypted with a per-profile key that lives in the
owner's pidvault vault and travels with each request from an unlocked shell:

- A1 can read neither the blob (the daemon's) nor the key (in the vault).
- An unlocked shell has the key but never the blob, so it cannot carry off the provider session.
- A copy of the disk has the blob but not the key, which is encrypted with the vault.
- The daemon holds the keep-set in plain text only while a booth for it runs.

### Delivering the token

Any of the owner's processes can open another's pipes through `/proc/<pid>/fd`; Yama
restricts attaching, not this. So:

1. **Over the socket** to a caller that speaks the protocol, such as a small library mushcam
   could use: a socket cannot be reopened through `/proc`, so the token reaches only the
   caller's memory.
2. **Into a vault file**, written by the client in the unlocked shell, e.g.
   `/run/pidvault/stored/default/tokens/gopro`. A consumer that reads it as a file is covered
   by pidvault's claim C1: every read is checked against the reader. Needs nothing new from
   pidvault, and fits mushcam's existing token file.
3. **On stdout**, for convenience, and exposed while in the pipe, as pidvault's
   `--passphrase-stdin` is. A token on a command line is worse: `/proc/<pid>/cmdline` is
   readable by every user.

### Showing the window as another user

A Wayland connection is a connected socket, and libwayland uses an inherited one when
`WAYLAND_SOCKET` names its fd. The client, which runs as the owner, connects to the
compositor and passes the connection to the daemon; the booth draws through it. The booth's
uid needs no access to the owner's `$XDG_RUNTIME_DIR`, and the compositor sees an ordinary
client of the owner's. A spike confirmed that QtWebEngine runs with this as its only route
to the display.

- **X11 is refused.** Any X client can read the keystrokes, so a booth there protects nothing typed.
- **GPU:** logind grants `/dev/dri` to the seat's user only, so the booth renders in software.
  Login pages do not mind.
- **Chromium's sandbox** needs user namespaces, which Ubuntu 24.04+ allows only to binaries
  with an AppArmor profile saying so; the booth needs one, as Chrome has.
- **Security keys** (`/dev/hidraw`) are likewise the seat user's; passkeys need a way through.

### Claims

- **T1. Outside an unlocked shell, the owner's processes can neither get a token nor open a
  booth.** The daemon checks the caller's uid and shell on every request, holding the peer by
  pidfd so a reused pid or a passed-on connection is refused. Opening a booth is gated too: a
  booth lends credibility to whatever page it shows.
- **T2. No process of the owner can read a stored keep-set**, not even in an unlocked shell.
  The blob is the daemon's, the booth that decrypts it has a uid of its own, and the client
  sees only tokens.
- **T3. A booth sees only its own run.** A fresh uid, in-memory browser state and a private
  `/tmp` mean one run cannot read another's state, another profile's, or another user's.
- **T4. A booth cannot be steered.** Profiles change only through requests from an unlocked
  shell; the booth's environment and arguments come from the daemon; no remote debugging is
  ever enabled; top-level navigation is limited to the profile's hosts.
- **T5. A compromised booth is contained.** A Chromium exploit (A3) gets a uid that owns
  nothing, in a sandboxed unit, holding only the one keep-set loaded for this run.
- **T6. Other users learn nothing** about an owner's profiles, tokens or state.

### Known limitations

- **The compositor is the owner's.** It runs as the owner, so A1 can stand between the booth
  and the screen: replace `$XDG_RUNTIME_DIR/wayland-0` with a proxy that records what is
  typed. The daemon can see who is at the far end of the passed connection, but proving it is
  the real compositor needs `/proc/<pid>/exe` of another uid's process, which needs
  `CAP_SYS_PTRACE` — pidvault's wall too; a small root helper could check it. A1 can also load
  code into the compositor itself, from GNOME Shell extensions or KWin scripts in the home
  directory, unless the administrator locks that (dconf `disable-user-extensions`).
- **A spoofed booth.** Any Wayland client can draw a window that looks like this one. The
  host strip is the booth's own; a lookalike has no way to be told apart by the compositor.
- **Pipes, the environment and command lines** expose a token handed on that way (see
  [Delivering the token](#delivering-the-token)).
- **Stay signed in stores the provider session.** For Google that is the whole account; T2
  keeps it from the owner's processes, but the daemon holds it.
- **What a provider allows.** Remember-the-device without staying signed in depends on the
  provider splitting the two.

### The prototype

Runs as the user, from a virtualenv, so A1 can read and change everything it has. With
`persist_session` it keeps a whole Chromium profile — including the identity provider's
session — under `<home>/sessions/`, readable by any of the user's processes. Tokens leave on
stdout. It does keep the measures that carry over: it refuses to start when
`QTWEBENGINE_REMOTE_DEBUGGING` is set, drops `QTWEBENGINE_CHROMIUM_FLAGS`, passes Qt no
command-line arguments (any of these could open a debugging port onto the logged-in session),
keeps the Chromium sandbox on, limits top-level navigation, and logs navigations without query
or fragment and cookie names without values.

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

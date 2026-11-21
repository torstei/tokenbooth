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

The booth is Python. By default it drives Google Chrome, an `--app` window controlled over
`--remote-debugging-pipe` (`tokenbooth/engines/chrome.py`); see
[Packaging the engine](#packaging-the-engine). The frame and host strip drawn above are the Qt
engine's (`--engine qt`, `tokenbooth/engines/qt.py`), QtWebEngine embedded through PySide6;
Chrome's app window has neither. For Qt, the pip wheels bundle a current Qt, so client-hint
control and WebAuthn are available; from 6.12, QtWebEngine is its own wheel,
`PySide6-WebEngine`, no longer part of `PySide6-Addons`.

## Home directory (prototype)

The prototype runs as the user and keeps everything under one directory, `--home`,
`$TOKENBOOTH_HOME`, or `~/.local/share/tokenbooth`:

```
<home>/profiles/<name>.toml   what to open and what to capture
<home>/keep/<name>.json       the kept cookies, see keep_cookies (0600)
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
keep_cookies    = []        # NAME@DOMAIN globs of cookies to carry into the next run
user_agent      = ""        # override; default is the engine's own (Qt's without "QtWebEngine")
timeout         = 600
```

Every key can also be given on the command line, so `tokenbooth-dev get foo --url https://… --cookie x`
works with no profile file. Without `allowed_hosts`, the start URL's host and its subdomains
are allowed.

`tokenbooth-dev discover NAME --url … [--via …]` finds the cookie for a new site. You sign in by
hand and close the window (with Qt, press **I'm logged in**; there, Cancel or closing the window
suggests nothing); it lists the cookies on the start URL's site (the host without
`www.`, and without its first label when three or more remain) in the order they appeared,
and prints a profile using the likeliest. A candidate is a cookie whose name first appeared
after the run left the start page and that is still set at the end, so a session id that
existed before sign-in and was merely replaced (`JSESSIONID`) or a flag that flipped
(`logged_in=no`) is never chosen. Among candidates, `HttpOnly`, a JWT value, and a name with
`access`, `token`, `session` or `auth` in it rank higher. Values are looked at, never kept.

Each run starts from an empty, off-the-record browser profile. `keep_cookies` names the
cookies carried over: `NAME` is a glob on the cookie's name, `DOMAIN` a host glob on its
domain without the leading dot, so `*@google.com` keeps every `.google.com` cookie and
`__Host-GAPS@accounts.google.com` one host-only cookie. They are harvested as the run sets
them and seeded into the next run's cookie store before its first navigation; a host-only
cookie is seeded for its host, since a `__Host-` cookie given a domain is refused. The token
cookie itself is never kept. `--keep` on the command line replaces the profile's list, and
`tokenbooth-dev keep-set NAME` lists what is kept, without values.

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
| `cookie` | Chrome: `Storage.getCookies`, polled; Qt: `QWebEngineCookieStore.cookieAdded`; both include HttpOnly cookies | session cookies (GoPro) — **prototype** |
| `localstorage` | `runJavaScript` in the right origin once the login is done | SPAs keeping a JWT in storage (MSAL.js, Auth0) |
| `header` | a script injected at document creation wraps `fetch`/XHR and reports `Authorization: Bearer …` | APIs where the token only travels in requests |
| `oauth` | a real OAuth client with PKCE; the redirect to `http://127.0.0.1/cb` is caught in `acceptNavigationRequest` and the code exchanged directly | services that let you register a client |

## The user agent and embedded-browser blocks

Google Chrome needs none of this ([shown](#what-the-prototype-has-shown), 9); it is the Qt
engine's problem. The user agent is ours to set. QtWebEngine's default contains
`QtWebEngine/x.y`; the booth removes it and otherwise reports the engine's real Chromium
version. The rest of the problem:

- **Client hints.** `Sec-CH-UA` and `navigator.userAgentData` must agree with the string
  (`QWebEngineClientHints`, Qt 6.8+). With PySide6 6.12 (Chromium 140) they already report
  plain `Chromium` with no Qt brand, and `navigator.webdriver` is false, so after the UA fix the
  window presents as a stock Chromium. Claiming to be Google Chrome would mean adding that brand.
  Claiming a browser that is not Chromium-based (Firefox, for Google) contradicts them, since
  only Chromium has client hints. The booth then removes `navigator.userAgentData` before any
  page script runs, in subframes too, and turns off the hints a site asks for with
  `Accept-CH`. The low-entropy `Sec-CH-UA`, `-Mobile` and `-Platform` headers still go out:
  QtWebEngine 6.12 lets a request interceptor blank them but not drop them, and an empty
  header is a stranger tell than an honest one.
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
     │  + a Wayland connection fd (SCM_RIGHTS)              caller's uid and pid
     │  + a connection to /run/pidvault.sock (SCM_RIGHTS) ──► asks pidvault over it: is the
     │  + the state key, read from the vault                caller's shell unlocked?
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
  profiles, authenticates callers by the kernel's credentials and a pidfd, and has pidvault
  say whether the caller's shell is unlocked (see [Asking pidvault](#asking-pidvault)). The
  client trusts the daemon only if it runs as another uid behind a root-owned socket
  directory, as pidvault's client does.
- **The booth** is a browser, Google Chrome — the largest attack surface by far — so it gets
  nothing it can keep. Each run is an instance of a socket-activated unit with `DynamicUser=yes`
  (`Accept=yes`, on a socket only the daemon can connect to): a uid of its own that owns
  nothing (`User=tb-%i`; `DynamicUser=` alone names the user after the template, so every
  instance would share one), a PID namespace of its own (`PrivatePIDs=yes`), so no booth can
  see, signal or enter another's processes, no `/home` (`ProtectHome=yes`; `DynamicUser=` alone
  leaves it listable), a private `/tmp`, a browser profile there, and a clean environment, so no `LD_PRELOAD`, `QT_*`,
  `PYTHON*` or `QTWEBENGINE_*` reaches it from the caller. It exits when the login completes
  or is cancelled, and everything it wrote goes with its uid.
- **The code is root-owned** under `/usr`, from a package, never a virtualenv in a home
  directory that A1 can rewrite.

### Asking pidvault

pidvault decides whether a process is in an unlocked shell for the process at the other end
of its control socket: on a `status` request it takes the connection's `SO_PEERCRED` pid and
walks its parents to a live, unexpired unlocked shell, reporting `unlocked_by` per vault.
`pidvault status` run by the daemon would describe the daemon, and one run by the client is
a claim by a process A1 can impersonate. So the question is asked over a connection the
caller opened:

1. The client connects to `/run/pidvault.sock` and passes that connection, with its request,
   to tokenboothd.
2. tokenboothd checks that the passed connection leads to pidvault, so the reply is
   pidvault's, then sends `{"op":"status"}` on it and reads the reply. A connection's peer
   credentials are those of whoever created the listening socket: the `pidvault` account, or,
   with pidvault socket-activated, systemd as root. A root listener counts only when its
   address is pidvault's socket path, a name only root can give it; a fake made by A1 has the
   owner's uid whatever name it binds, in whatever namespace.
3. The profile's vault (`vault`, default `default`) must show `unlocked_by`, and the client
   must descend from that shell and share its uid. pidvault answered for whoever opened the
   connection; that vouches for the client only from inside the same shell, so an unlocked
   shell's connection passed to a process outside it vouches for nothing.
4. Afterwards the client's pidfd, taken by `SO_PEERPIDFD` on its connection to tokenboothd,
   must not have exited. While the client lives its pid cannot be reused, so the process
   pidvault judged was the client and not whatever took over the pid of an A1 that opened a
   connection, passed it on and exited.

A1 cannot do better: a connection it opens carries its own pid, and a socket cannot be
reopened through `/proc/<pid>/fd`, so it cannot borrow one from an unlocked shell. The check is
pidvault's own, live: it ends when the shell locks, exits or its unlock expires.

pidvault 0.6 documents this use of `status` as a contract for other programs ("Asking
pidvault from another program" in its security design) and itself holds the control-socket
peer by pidfd, refusing once the process that opened the connection has exited (its claim
C8), so step 4 is a second line of defence. tokenbooth therefore needs pidvault 0.6 or later,
and Linux 6.5 or later for `SO_PEERPIDFD`. The contract's own advice, to check that the passed
socket's peer is the `pidvault` account, cannot hold for pidvault's socket-activated
installation, where the peer is systemd (pid 1, uid 0); hence the socket-path test in step 2.

The state key in the vault is still needed, to encrypt the keep-set (T2), not to prove who is
asking.

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
  with an AppArmor profile saying so. The namespaces are created by the browser process — the
  booth's interpreter, not `QtWebEngineProcess` — so Ubuntu's `QtWebEngineProcess` profile does
  not help. The booth runs from an executable of its own, `/usr/lib/tokenbooth/booth` (a copy
  of the interpreter or a launcher; not a symlink, since AppArmor attaches by the resolved
  path), with a profile that grants `userns` to that path alone. Without it, QtWebEngine
  carries on with seccomp only.
- **Security keys** (`/dev/hidraw`) are likewise the seat user's; passkeys need a way through.

### Claims

- **T1. Outside an unlocked shell, the owner's processes can neither get a token nor open a
  booth.** The daemon checks the caller's uid on every request and has pidvault check its
  shell over a connection the caller opened, holding the peer by pidfd so a reused pid or a
  passed-on connection is refused. Opening a booth is gated too: a
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

Runs as the user, from a virtualenv, so A1 can read and change everything it has. Its
keep-set is a plain file under `<home>/keep/`, readable by any of the user's processes; when
it holds an identity provider's session cookies, so does that file. Tokens leave on stdout.
It does keep the measures that carry over. The booth is a child process spoken to in the
[booth protocol](docs/booth-protocol.md), draws only through Wayland, limits top-level
navigation, redirects included, and logs navigations without query or fragment and cookie
names without values. Chrome is controlled only over its pipe, never a port, and starts with an
environment of its own: its temporary profile as `HOME` and `XDG_RUNTIME_DIR`, so neither the
owner's certificate store under `~/.pki` nor the owner's D-Bus is reached. Its sandbox is fully
on, through Ubuntu's AppArmor profile for Chrome. The Qt engine refuses to start when
`QTWEBENGINE_REMOTE_DEBUGGING` is set, drops `QTWEBENGINE_CHROMIUM_FLAGS` and passes Qt no
command-line arguments (any of these could open a debugging port onto the logged-in session);
its Chromium sandbox is only half on: renderers get seccomp, but AppArmor refuses the venv's
interpreter the user namespaces the namespace layer needs (see
[Showing the window as another user](#showing-the-window-as-another-user)).

## Passkeys and security keys

Qt 6.7 added WebAuthn (`QWebEngineWebAuthUxRequest`); the application draws the PIN and touch
prompts itself, and needs access to the key's `/dev/hidraw` device. USB keys should work;
signing in with a passkey on a phone probably does not. Not in the prototype.

## What the prototype has shown

1. **GoPro works**, signing in with Google: the embedded window completes the login and
   captures `gp_access_token`, a 7-day cookie, so `tokenbooth get` answers from the cache for a week.
2. **Google** refuses the embedded browser with a clean Chromium user agent ("This browser or
   app may not be secure") and accepts it with a Firefox one, the same trick qutebrowser uses —
   but only a current Firefox: claiming Firefox 140 when 158 was current got the same refusal,
   from the old and the new code alike, and claiming 158 got through at once. The version is
   counted from a known release in whole four-week cycles, so it never claims one not yet out.
   Phone-prompt 2-step verification works. After sign-in, Google bounces the top-level page
   through `accounts.youtube.com` and `accounts.google.<country>` (`SetSID`) to set cookies
   there. `--via google` / `via = ["google"]` bundles those hosts and the user agent.
   If Google stops accepting this, the fallback is a second engine: the real Chrome binary on
   its own profile, controlled over `--remote-debugging-pipe` (reachable only by tokenbooth), so
   that Google sees genuine Chrome ([Packaging the engine](#packaging-the-engine), P3).
3. **Host globs:** `*` matches exactly one DNS label and only a leading `*.` matches any depth,
   so `accounts.google.*` admits `accounts.google.no` but not `accounts.google.evil.com`.
4. **A booth runs as a dynamic uid and draws through a passed connection** (spike S1,
   `spikes/s1-booth-unit/`). A socket-activated `Accept=yes` template with `DynamicUser=yes`
   receives the Wayland fd and the request over `SCM_RIGHTS`; with `WAYLAND_SOCKET` set and
   `WAYLAND_DISPLAY`/`DISPLAY` removed, Qt draws through it alone and loads GoPro's login page.
   The compositor sees a client of the owner's uid; the booth cannot list the owner's
   `$XDG_RUNTIME_DIR`, and its environment holds only what the unit sets and systemd's own
   variables. Rendering is software (`/dev/dri` is denied), about 400 MB at peak, with no D-Bus.
5. **Chromium's full sandbox runs in that unit** (spike S2, `spikes/s2-sandbox/`) once the
   interpreter is a copy at its own path with an AppArmor profile granting `userns`: the
   sandboxed zygote and the renderers get PID namespaces of their own, and the renderers a
   seccomp filter of Chromium's on top of systemd's. With the system interpreter the user
   namespace is moved into `unprivileged_userns`, `CAP_SYS_ADMIN` is denied, and only seccomp
   remains — as in the prototype.
6. **Google, kept cookies** (spike S3, GoPro through Google, from an off-the-record profile):
   - Seeded with no cookies, the login asks for the password and the second factor: nothing
     but cookies makes Google remember the device.
   - Keeping the 21 cookies set on `google.com` and its subdomains makes the next login an
     account choice and the consent screen, with no password and no second factor: the
     **stay signed in** level works.
   - Google's refusals ("This browser or app may not be secure", "Could not sign you in")
     are judged per attempt. After several sign-ins in an hour, a cookie-less login that had
     gone straight through earlier was refused, and **Try again** (`accounts.google.com/restart`,
     already an allowed host) then led on to the password. A single refusal therefore says
     nothing about a keep-set; each candidate needs a retry before it is ruled out.
   - Keeping only the six cookies outside the session (`ACCOUNT_CHOOSER`, `NID`, `OTZ`,
     `SMSV`, `__Host-GAPS`, `__Secure-ENID`) offered the account and was then refused, without
     a retry; whether **remember the device** can be split from the session is open.
   - Every refusal came before the booth hid `navigator.userAgentData` and the requested client
     hints when claiming Firefox. The three Google sign-ins after it (two account choices from
     a kept session, one full sign-in with 2-step) went through without a refusal or a retry.
     Whether that is the fix or a calmer risk score is not settled.
7. **Microsoft Entra** (a work app signing in through an Entra tenant, `--via microsoft`):
   - The embedded browser is accepted as it is, with QtWebEngine's own Chromium user agent:
     password and MFA went through without objection.
   - Keeping the cookies set on `login.microsoftonline.com` and `login.live.com`, among them
     `ESTSAUTH` and `ESTSAUTHPERSISTENT`, makes the next login silent: the authorize request
     redirects straight back to the app, with no sign-in page, password or MFA. The **stay
     signed in** level works here too, and it is the whole Microsoft 365 account that is kept.
   - The app's own session arrives as cookies on its domain after the redirect back, so the
     token to capture is one of those, not anything at Microsoft.
8. **GitHub, signing in through Google** (`--via google`): the whole chain works in the booth.
   - After Google, GitHub asks for its own second factor (the push to its mobile app), even
     when Google needed only an account choice.
   - `tokenbooth discover` picks `user_session`, tied with `__Host-user_session_same_site`,
     which carries the same value. `saved_user_sessions`, set just before them, holds a user
     id with the token and is passed over because its value is not opaque.
   - `user_session` is the whole web session of the account. GitHub lets you register OAuth
     apps and issue scoped tokens, which serve an API far better, so GitHub shows that the
     booth works rather than being a site to harvest.

9. **Google Chrome runs as the booth** (spike S5, `spikes/s5-chrome/`, Chrome 158), in the S1
   unit with `ProtectHome=yes`, drawing through the passed Wayland fd and driven only over
   `--remote-debugging-pipe`:
   - Chrome opens a second Wayland connection for GTK. With only the passed one, GTK fails, Chrome
     falls back to its Qt shim, and Qt aborts the process. With the shim hidden
     (`InaccessiblePaths=` on `/opt/google/chrome/libqt*_shim.so`) the fallback fails cleanly and
     Chrome runs with no toolkit.
   - The sandbox needs nothing of ours: Ubuntu's `/etc/apparmor.d/chrome` grants Chrome `userns`.
     Every renderer has its own PID namespace and Chrome's seccomp filter on top of the unit's.
     No TCP socket listens and no `DevToolsActivePort` is written.
   - Rendering is software, and as the dynamic uid WebGL is unavailable (`null`), since Chrome no
     longer falls back to a software WebGL. Google's sign-in did not object.
   - Driven over the pipe, Chrome sets `navigator.webdriver`, and Google refuses sign-in ("This
     browser or app may not be secure"), retries included. `--disable-blink-features=AutomationControlled`
     turns the flag off in the engine, and Google then accepts Chrome's own user agent and
     client hints: no Firefox claim and no script.
   - Chrome reports every web sign-in to Google (`X-Chrome-ID-Consistency-Request`), and a Chrome
     holding no Google token of its own is then shown a kept session as signed out: the account
     listed, the password asked, the second factor spared. The same cookies, played into a
     Chrome without it, are signed in. Seeding the new profile's `Preferences` with
     `signin.allowed` and `signin.allowed_on_next_startup` false stops it; the second is the one
     read at startup. Google also offers Chrome a bound session
     (`Sec-Session-Google-Registration-List`); the booth turns `EnableBoundSessionCredentials` off,
     which was not shown to matter.
   - The keep-set works through `Storage.getCookies` and `Storage.setCookies`, host-only cookies
     given by URL; the **stay signed in** level works as with Qt WebEngine.
   - What Chrome does not give: the window has no frame or host strip of the booth's own (an
     `--app` window shows the page title), and F12 still opens DevTools in it.

10. **The pidvault check holds** (spike S4, `spikes/s4-pidvault/check.py`, pidvault 0.6.0), run
    from a shell with a test vault unlocked against the installed daemon:
    - A connection the client opened itself is answered with the vault unlocked.
    - A connection opened by a process that then passed it on and exited is refused by pidvault
      itself ("the process that opened this connection has exited").
    - The shell's connection used by a process that has left the shell's process tree, and such a
      process's own connection, are both answered with nothing unlocked.
    - A connection to pidvault's socket-activated control socket has systemd (pid 1, uid 0) as
      its peer, never the `pidvault` account, so the peer is told apart by uid 0 and its address.

Still open: whether **remember the device** can be split from the session, for Google and for
Entra; whether GitHub's `_device_id` spares its second factor; how a Chrome booth shows where
you are typing without a host strip of its own; then the agent and how the commands should look.

## Implementation plan

From the prototype to the [security model](#security-model), in steps that each end in
something that runs. Steps 1 and 3 can proceed in parallel once the booth's request format
(1.3) is fixed; the daemon is built against a stub booth until then.

The booth stays runnable on its own, as the user and without the daemon, the socket or a
dynamic uid: `tokenbooth-dev login` and `tokenbooth-dev discover` from a checkout. That is how sites
are explored, keep-sets narrowed and the booth tested, with the prototype's guarantees and
none of the [security model](#security-model)'s; nothing a run like that holds should be
trusted to it.

### Packaging the engine

The booth renders third-party login pages, so its Chromium needs security fixes as soon as
Chrome gets them, sandbox or not. Whoever ships that Chromium owns that duty. Everything else
in the package is small and ours: the daemon and client, the booth launcher
`/usr/lib/tokenbooth/booth` and its AppArmor profile, the systemd units and the site
descriptions.

| | Engine | The package | Who ships Chromium fixes |
|---|---|---|---|
| P1 | The distribution's Qt WebEngine | Small; depends on `python3-pyside6.qtwebenginewidgets` | The distribution. Ubuntu carries it in universe, community-maintained, a Qt release or more behind; Debian's release notes list qtwebengine as having limited security support. Ubuntu 24.04's Qt 6.4 is too old for client-hint control. |
| P2 | PySide6 wheels vendored under `/usr/lib/tokenbooth`, pinned with hashes | Large: the wheels are about 670 MB, Chromium alone about 200 MB | Us: a tokenbooth release for every Qt patch release that carries Chromium fixes |
| P3 | Google Chrome from Google's apt repository, driven over `--remote-debugging-pipe` | Small; depends on `google-chrome-stable` | Google, through apt. Ubuntu's `chromium-browser` is a snap and cannot run inside the booth unit. |

P3 is the intended default for a package, so tokenbooth never ships Chromium itself; it also
lets Google see a genuine Chrome, and the Firefox user agent and client-hint script can go.
The DevTools protocol covers what the booth needs: `Network.setCookies` and
`Network.getCookies` for the keep-set, the user agent override, scripts added before any page
script runs, and navigations held at the request stage to enforce the host allowlist. The
locked-down window itself becomes switches and protocol calls instead of Qt code: `--app=` for
a window without an address bar, `Browser.setDownloadBehavior` to deny downloads, permission
requests refused. Chrome's managed policies are not an option, since `/etc/opt/chrome/policies`
would bind the owner's own Chrome as well; what a policy would set goes into the fresh
profile's `Preferences` instead, as `signin.allowed_on_next_startup` does. The booth unit hides
Chrome's Qt shim, and the booth starts Chrome with `--disable-blink-features=AutomationControlled`
([shown](#what-the-prototype-has-shown), 9). Qt WebEngine (P1 or a venv) stays
the engine for development and experiments. Only the modules under `tokenbooth/engines/` know
which engine runs; profiles, the keep-set and discovery do not, and the booth protocol carries
no engine. P3 adds Google to [A5](#adversaries).

### Spikes first

Each of these could change the plan, so each is settled before the code that depends on it.

| | Question | Settled when |
|---|---|---|
| S1 | Can a booth unit receive the Wayland fd? `systemd-run` cannot hand a transient unit an arbitrary fd. Candidate: a socket-activated `tokenbooth-booth@.service` with `Accept=yes` and `DynamicUser=yes`, on a socket only the tokenbooth uid can connect to; the daemon connects and sends the Wayland fd and the request over `SCM_RIGHTS`, and the booth sets `WAYLAND_SOCKET` before Qt starts. | Settled: yes ([shown](#what-the-prototype-has-shown), 4) |
| S2 | Does Chromium's sandbox run under that unit, with an AppArmor profile allowing user namespaces, and does software rendering suffice? | Settled: yes, with an interpreter of the booth's own ([shown](#what-the-prototype-has-shown), 5) |
| S3 | Which Google cookies remember the device without keeping the session, and does `setCookie` into an off-the-record profile before the first navigation make the next login skip 2-step verification? | A keep-set for each of the three levels, or the finding that remember-the-device cannot be split off |
| S4 | Does the pidvault check in [Asking pidvault](#asking-pidvault) hold, including the pid-reuse case? (`spikes/s4-pidvault/check.py`) | Settled: yes ([shown](#what-the-prototype-has-shown), 10) |
| S5 | Can the booth unit run Google Chrome (P3) as S1 and S2 run Qt WebEngine: as a dynamic uid with no home, on the passed Wayland fd (`--ozone-platform=wayland`), with its sandbox on under an AppArmor profile, controlled only over `--remote-debugging-pipe`; and do the GoPro login and the keep-set work through the DevTools protocol with Chrome's own user agent? | Settled: yes, with automation flags and Chrome's account sign-in turned off ([shown](#what-the-prototype-has-shown), 9) |

### Steps

| Step | What | Done when |
|---|---|---|
| 1 | **The booth on its own**, still run as the user | |
| 1.1 | Off-the-record profile only; `persist_session` and `<home>/sessions` go | GoPro login works with no files written but the token |
| 1.2 | Keep-set in through `setCookie`, out through `cookieAdded`, filtered by `keep_cookies` | S3 answered with the booth itself |
| 1.3 | Framed request and response on an fd instead of argv, files and stdout | The format is written down ([the booth protocol](docs/booth-protocol.md)) and both ends of the tests use it |
| 1.4 | Refuses to start without Wayland, so never on X11 | |
| 1.5 | The current CLI becomes a thin wrapper around the booth | `tokenbooth get gopro` works as before |
| 2 | **The booth as a dynamic uid** (S1, S2, S5): `systemd/`, the code bound read-only from a checkout until step 4; the caller passes its Wayland connection with the request | Step 1's booth runs as `tokenbooth-booth@` |
| 3 | **Daemon and client**, in Rust | |
| 3.1 | Socket, `SO_PEERCRED` and `SO_PEERPIDFD`, state under `/var/lib/tokenbooth/<uid>/`; the client and daemon are one binary, `tokenbooth`, as pidvault is | `tokenbooth status` reports the caller the kernel saw |
| 3.2 | The pidvault check (S4); profiles change only through checked requests: `tokenbooth profile set\|show\|list\|delete`, each against the profile's vault | Outside an unlocked shell, no profile can be read, listed or changed |
| 3.3 | Keep-set encrypted with the key from the vault; the token cache | |
| 3.4 | Token delivery: over the socket, into a vault file, on stdout | |
| 3.5 | The client refuses a daemon of its own uid or outside a root-owned socket directory | |
| 4 | **Packaging**: `.deb`, sysusers, the socket and `DynamicUser` template units with `ProtectHome=yes`, the booth executable `/usr/lib/tokenbooth/booth` and its AppArmor profile, Google Chrome as the engine (P3, [S5](#spikes-first)) | A clean VM installs it, `tokenbooth get gopro` works, and the renderer has its own PID namespace |
| 5 | **pidvault**: `status` and `unlocked_by` documented as a contract with a test; the control-socket peer held by pidfd | |
| 6 | **mushcam** reads the token from a vault file or through a small socket library | `mushcam` no longer asks for a pasted cookie |

### Testing

| | Level | How |
|---|---|---|
| 1 | Unit | |
| 1.1 | | pytest: `host_matches`, profile validation, `via` expansion, the keep-set filter, the request format |
| 1.2 | | `cargo test`: daemon messages, keep-set encryption round trip, every authorisation decision |
| 2 | Booth against a fake site, no human | |
| 2.1 | | A local HTTPS server whose test CA is in an NSS database under the test's own `$HOME`, so the booth needs no switch for certificate errors |
| 2.2 | | The fake pages script themselves: submit the login, set the cookie, open a popup that talks to its opener, navigate to a host outside the allowlist, start a download, ask for the camera |
| 2.3 | | Asserted: the token came out, blocked navigations were blocked, permissions refused, the keep-set holds exactly the named cookies |
| 2.4 | | Run under a headless Weston, so the `WAYLAND_SOCKET` path is exercised too |
| 3 | The claims, in a VM or a `systemd-nspawn` container with real systemd | |
| 3.1 | T1 | Callers outside an unlocked shell, with a passed-on pidvault connection, and with a reused pid are refused |
| 3.2 | T2 | A process of the owner, unlocked or not, cannot read the stored keep-set |
| 3.3 | T3, T5 | From inside a booth: the owner's home, another booth's `/tmp` and the daemon's state are unreadable; `systemd-analyze security` on the booth unit stays under a set score |
| 3.4 | T4 | `LD_PRELOAD`, `QT_*`, `QTWEBENGINE_*` and `PYTHON*` set on the caller do not reach the booth |
| 3.5 | T6 | A second user gets the same refusal whether or not a profile of that name exists |
| 4 | Real logins, by hand | GoPro through Google at each keep level, recording which cookies made the difference |

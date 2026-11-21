# The booth protocol

How a caller asks a booth for one login. The caller is the `tokenbooth` command today and
`tokenboothd` later ([DESIGN.md](../DESIGN.md#components)); the booth is
`python -m tokenbooth.booth [--engine chrome|qt] [FD]`. The engine is the booth's choice, made
where it is started; nothing in the protocol names it. Implemented in `tokenbooth/protocol.py` (both ends) and
`tokenbooth/booth.py` (the booth's).

## Connection

- A connected `AF_UNIX` `SOCK_STREAM` socket. The booth finds its end on the fd given as its
  only argument, default 3, where systemd puts a socket-activated `Accept=yes` connection.
- One login per connection. The booth exits after sending the result.
- **Hanging up cancels.** When the caller closes its end, the booth closes its window and exits.
  The caller sends nothing after the request, so anything readable on the booth's end counts as
  a hang-up.

## Frames

Each message is one frame: a 4-byte big-endian length, then that many bytes of UTF-8 JSON
holding an object with a string `type`.

- A frame is at most 1 MiB (1 048 576 bytes).
- File descriptors travel as `SCM_RIGHTS` with a frame's first bytes, at most 4 per frame.
  Version 1 defines none, and a booth closes any it receives. The Wayland connection will be the
  first.
- Every key of a request is required, and a request or result with a key not listed here is
  refused, so a change to either is a new `version`.

## Sequence

```
caller                                   booth
  │── request ─────────────────────────────►│
  │◄──────────────────────────────── log ───│  zero or more, only if requested
  │◄───────────────────────────── result ───│  exactly one, last; then the booth exits
```

A request that cannot be parsed or is refused gets a `result` with outcome `refused` and
nothing else.

## `request`

| Key | Type | |
|---|---|---|
| `type` | `"request"` | |
| `version` | `1` | |
| `mode` | `"login"` or `"discover"` | `discover` waits for the window to be closed (Qt: "I'm logged in") and reports cookies instead of capturing one |
| `log` | boolean | send `log` events |
| `profile` | object | below |
| `keep` | array of keep entries | the keep-set to seed before the first navigation |

`profile` is resolved by the caller: the booth reads no files and knows no identity providers.

| Key | Type | |
|---|---|---|
| `name` | string | shown in the window title |
| `start_url` | string | `https://` only |
| `allowed_hosts` | array of strings | host globs for top-level navigation, any `via` already expanded |
| `cookie` | string | the cookie holding the token; may be empty only in `discover` |
| `cookie_domain` | string | the domain the cookie must belong to, or a subdomain of it |
| `done_url` | string | regex the page URL must match before the cookie counts; empty for none |
| `keep_cookies` | array of strings | `NAME@DOMAIN` globs of cookies to return in the keep-set |
| `user_agent` | string | empty for the engine's own, without its engine marker |
| `timeout` | integer | seconds; 0 for none |

A keep entry:

| Key | Type | |
|---|---|---|
| `name`, `domain`, `path` | string | the cookie's; `domain` with its leading dot when it has one |
| `expires` | number or null | Unix time; null for a session cookie |
| `raw` | string | the cookie as the booth's engine stores it, base64: for Chrome a DevTools `Network.Cookie` object in JSON, for Qt a `Set-Cookie` line; opaque to the caller |

The caller stores keep entries and hands them back unchanged. It may read the other keys to list
entries or drop expired ones, never `raw`. A booth ignores an entry whose `raw` it cannot read,
so a keep-set written by one engine is merely not seeded into another.

## `log`

| Key | Type | |
|---|---|---|
| `type` | `"log"` | |
| `text` | string | navigations without query or fragment, cookie names without values |

## `result`

| Key | Type | |
|---|---|---|
| `type` | `"result"` | |
| `outcome` | string | below |
| `token` | string or null | the captured cookie's value; a string exactly when `outcome` is `token` |
| `expires` | number or null | the cookie's expiry, Unix time; null for a session cookie |
| `keep` | array of keep entries | the cookies matching `keep_cookies` when the run ended, never the token cookie; empty when `keep_cookies` is |
| `cookies` | array | `discover` only: every cookie the run saw, without values (below) |
| `error` | string | `refused` only: why |

| `outcome` | |
|---|---|
| `token` | the cookie appeared, after `done_url` if there is one |
| `confirmed` | `discover`: the window was closed (Qt: "I'm logged in" was pressed) |
| `cancelled` | `login`: the window was closed; either mode: the caller hung up (Qt: also Cancel or closing in `discover`) |
| `timed_out` | `timeout` passed |
| `refused` | the request was malformed, or the booth will not run, e.g. it has no Wayland display, Chrome is missing or exited before opening its window, or a debugging port is requested in its environment |

A `cookies` entry describes one cookie by `name`, `domain` and `path`:

| Key | Type | |
|---|---|---|
| `order` | integer | in which it first appeared |
| `before` | boolean | first set before the run left the start page |
| `present` | boolean | still set at the end |
| `http_only`, `secure`, `session` | boolean | its attributes |
| `length` | integer | the length of its value |
| `jwt`, `opaque` | boolean | whether its value looks like a JWT, or a long opaque string |

## Exit status of the booth

0 after sending a result for a run, 1 when the caller hung up before sending a request, 2 after
refusing one.

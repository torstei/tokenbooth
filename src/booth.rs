//! The daemon's side of a booth: the request for a stored profile, and the conversation with a
//! booth unit while the caller waits (docs/booth-protocol.md).

use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};

use crate::frame::{self, Message};
use crate::profile::{Profile, host_of};

/// Hosts each identity provider's sign-in moves through; keep in step with `PROVIDERS` in
/// tokenbooth/store.py.
fn provider_hosts(provider: &str) -> &'static [&'static str] {
    match provider {
        "google" => &["accounts.google.*", "accounts.google.co.*", "accounts.google.com.*", "accounts.youtube.com"],
        "microsoft" => &["login.microsoftonline.com", "login.microsoft.com", "login.live.com"],
        _ => &[],
    }
}

/// The booth request for `profile`, resolved as `tokenbooth-dev` resolves one: without
/// `allowed_hosts`, the start host and its subdomains; the identity providers' hosts added.
pub fn request(name: &str, profile: &Profile, keep: &[Value], log: bool) -> Value {
    let host = host_of(&profile.start_url).unwrap_or_default().to_ascii_lowercase();
    let base = host.strip_prefix("www.").unwrap_or(&host).to_owned();
    let mut allowed = profile.allowed_hosts.clone().unwrap_or_else(|| vec![base.clone(), format!("*.{base}")]);
    for provider in &profile.via {
        allowed.extend(provider_hosts(provider).iter().map(|h| (*h).to_owned()));
    }
    json!({
        "type": "request",
        "version": 1,
        "mode": "login",
        "log": log,
        "profile": {
            "name": name,
            "start_url": profile.start_url,
            "allowed_hosts": allowed,
            "cookie": profile.cookie,
            "cookie_domain": profile.cookie_domain.clone().unwrap_or(base),
            "done_url": profile.done_url,
            "keep_cookies": profile.keep_cookies,
            "user_agent": profile.user_agent,
            "timeout": profile.timeout,
        },
        "keep": keep,
    })
}

pub enum Relayed {
    /// The booth's result.
    Done(Message),
    /// The caller hung up; the booth was hung up on in turn, which closes its window.
    CallerGone,
}

/// Hands the request and the caller's Wayland connection to a booth, forwards its log lines to
/// the caller when `forward_logs`, and returns its result.
pub fn run(booth_socket: &Path, request: &Value, wayland: OwnedFd, caller: &UnixStream, forward_logs: bool) -> Result<Relayed> {
    let booth = UnixStream::connect(booth_socket).with_context(|| format!("cannot reach the booth at {}", booth_socket.display()))?;
    frame::send(&booth, request, &[wayland.as_raw_fd()])?;
    drop(wayland);
    caller.set_read_timeout(None)?;
    loop {
        let mut fds = [
            libc::pollfd { fd: booth.as_raw_fd(), events: libc::POLLIN, revents: 0 },
            libc::pollfd { fd: caller.as_raw_fd(), events: libc::POLLIN, revents: 0 },
        ];
        // SAFETY: fds outlives the call and its length is the count given
        if unsafe { libc::poll(fds.as_mut_ptr(), 2, -1) } < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error.into());
        }
        // the caller sends nothing more, so anything readable on its end is it hanging up
        if fds[1].revents != 0 {
            return Ok(Relayed::CallerGone);
        }
        if fds[0].revents == 0 {
            continue;
        }
        let Some((message, _)) = frame::receive(&booth)? else {
            bail!("the booth closed the connection without a result");
        };
        match message["type"].as_str() {
            Some("log") if forward_logs => {
                if frame::send(caller, &Value::Object(message), &[]).is_err() {
                    return Ok(Relayed::CallerGone);
                }
            }
            Some("result") => return Ok(Relayed::Done(message)),
            _ => {}
        }
    }
}

/// The earliest of a cookie's expiry and, when the token is a JWT, its `exp`.
pub fn expiry(token: &str, cookie_expires: Option<f64>) -> Option<f64> {
    use base64::Engine;
    let jwt = token.split('.').collect::<Vec<_>>();
    let from_jwt = (jwt.len() == 3)
        .then(|| base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(jwt[1].trim_end_matches('=')).ok())
        .flatten()
        .and_then(|payload| serde_json::from_slice::<Value>(&payload).ok())
        .and_then(|claims| claims["exp"].as_f64());
    [cookie_expires.filter(|e| *e > 0.0), from_jwt].into_iter().flatten().reduce(f64::min)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile(text: &str) -> Profile {
        Profile::parse(text).unwrap()
    }

    #[test]
    fn a_profile_resolves_like_tokenbooth_dev() {
        let p = profile("start_url = \"https://www.gopro.com/login\"\ncookie = \"t\"\nvia = [\"google\"]\nkeep_cookies = [\"*@google.com\"]");
        let request = request("gopro", &p, &[], true);
        let hosts: Vec<_> = request["profile"]["allowed_hosts"].as_array().unwrap().iter().map(|h| h.as_str().unwrap()).collect();
        assert_eq!(&hosts[..2], ["gopro.com", "*.gopro.com"]);
        assert!(hosts.contains(&"accounts.youtube.com"));
        assert_eq!(request["profile"]["cookie_domain"], "gopro.com");
        assert_eq!(request["profile"]["user_agent"], "");
        assert_eq!(request["log"], true);
    }

    #[test]
    fn explicit_hosts_and_cookie_domain_win() {
        let p = profile("start_url = \"https://x.example/\"\ncookie = \"t\"\nallowed_hosts = [\"x.example\"]\ncookie_domain = \"api.example\"");
        let request = request("x", &p, &[], false);
        assert_eq!(request["profile"]["allowed_hosts"], json!(["x.example"]));
        assert_eq!(request["profile"]["cookie_domain"], "api.example");
    }

    #[test]
    fn a_jwt_expires_no_later_than_its_exp() {
        let jwt = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.c2ln";
        assert_eq!(expiry(jwt, None), Some(4102444800.0));
        assert_eq!(expiry(jwt, Some(2e9)), Some(2e9));
        assert_eq!(expiry("opaque", Some(2e9)), Some(2e9));
        assert_eq!(expiry("opaque", None), None);
    }
}

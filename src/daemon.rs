//! `tokenbooth daemon`: runs as the `tokenbooth` user, serves one request per connection, and
//! knows each caller by the kernel's credentials for the connection.

use std::collections::HashSet;
use std::fs::DirBuilder;
use std::os::fd::OwnedFd;
use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};

use crate::booth::{self, Relayed};
use crate::frame::{self, Message};
use crate::pidvault::{self, Unlocked};
use crate::procs::{self, PeerCred, Pidfd};
use crate::profile::{Profile, Store};
use crate::sealed::{Key, Loaded, State, Vaulted};

const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
/// A cached token counts only with at least this long left.
const EXPIRY_MARGIN: f64 = 60.0;

#[derive(Clone)]
pub struct Config {
    /// Holds one directory per caller's uid.
    pub state: PathBuf,
    /// A passed connection must lead to this pidvault.
    pub pidvault: pidvault::Pidvault,
    /// Where booths are started.
    pub booth_socket: PathBuf,
}

pub fn run(listener: UnixListener, config: Config) -> Result<()> {
    procs::harden().context("cannot make the daemon undumpable")?;
    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        let config = config.clone();
        std::thread::spawn(move || {
            if let Err(e) = serve(&stream, &config) {
                let _ = frame::send(&stream, &json!({"type": "error", "message": format!("{e:#}")}), &[]);
            }
        });
    }
    Ok(())
}

/// Who is asking, as the kernel saw the connection being opened.
struct Caller {
    cred: PeerCred,
    opener: Pidfd,
}

impl Caller {
    fn of(stream: &UnixStream) -> Result<Caller> {
        let cred = procs::peer_cred(stream).context("cannot read the caller's credentials")?;
        let opener = procs::connected_pidfd(stream).context("cannot hold the caller by pidfd (Linux 6.5 or later)")?;
        Ok(Caller { cred, opener })
    }

    /// An answer worked out from the caller's pid counts only while that process lives:
    /// after it exits, the pid can belong to anyone.
    fn still_there(&self) -> Result<()> {
        if self.opener.has_exited() {
            bail!("the process that opened this connection has exited");
        }
        Ok(())
    }

    /// Asks pidvault, over the connection the caller passed, which vaults its shell has
    /// unlocked. Every request that touches profiles or logins needs this.
    fn unlocked(&self, connection: Option<OwnedFd>, config: &Config) -> Result<Unlocked> {
        let connection = connection.context("pass a connection to pidvault with the request")?;
        let unlocked = pidvault::ask(connection, self.cred, &config.pidvault)?;
        self.still_there()?;
        Ok(unlocked)
    }
}

fn text<'a>(request: &'a Message, key: &str) -> Result<&'a str> {
    request.get(key).and_then(Value::as_str).with_context(|| format!("the request lacks {key}"))
}

/// The one fd a request other than a login may carry: a connection to pidvault.
fn only_pidvault(mut fds: Vec<OwnedFd>) -> Result<Option<OwnedFd>> {
    if fds.len() > 1 {
        bail!("pass one fd with the request: a connection to pidvault");
    }
    Ok(fds.pop())
}

fn serve(stream: &UnixStream, config: &Config) -> Result<()> {
    let caller = Caller::of(stream)?;
    stream.set_read_timeout(Some(REQUEST_TIMEOUT))?;
    let Some((request, fds)) = frame::receive(stream)? else {
        return Ok(());
    };
    let state = state_dir(&config.state, caller.cred.uid)?;
    if request["type"] == "login" {
        return login(stream, &caller, &request, fds, config, &state);
    }
    let fd = only_pidvault(fds)?;
    let reply = match request["type"].as_str() {
        Some("status") => {
            let vaults = match fd {
                None => Value::Null,
                Some(fd) => json!(caller.unlocked(Some(fd), config)?.unlocked.keys().collect::<Vec<_>>()),
            };
            caller.still_there()?;
            json!({"type": "status", "uid": caller.cred.uid, "pid": caller.cred.pid, "state": state,
                   "unlocked": vaults})
        }
        Some("profile-list") => {
            let unlocked = caller.unlocked(fd, config)?;
            let store = Store::open(&state)?;
            let mut profiles = Vec::new();
            for name in store.names()? {
                if let Ok(Some((_, profile))) = store.load(&name)
                    && unlocked.contains(&profile.vault)
                {
                    profiles.push(json!({"name": name, "vault": profile.vault}));
                }
            }
            json!({"type": "profiles", "profiles": profiles})
        }
        Some("profile-show") => {
            let unlocked = caller.unlocked(fd, config)?;
            let name = text(&request, "name")?;
            let (text, profile) = Store::open(&state)?.load(name)?.with_context(|| format!("no profile {name:?}"))?;
            unlocked.require(&profile.vault)?;
            json!({"type": "profile", "name": name, "toml": text})
        }
        Some("profile-set") => {
            let unlocked = caller.unlocked(fd, config)?;
            let name = text(&request, "name")?;
            let toml = text(&request, "toml")?;
            let profile = Profile::parse(toml)?;
            unlocked.require(&profile.vault)?;
            let store = Store::open(&state)?;
            if let Some((_, old)) = store.load(name)? {
                unlocked.require(&old.vault)?;
            }
            store.save(name, toml)?;
            json!({"type": "ok"})
        }
        Some("profile-delete") => {
            let unlocked = caller.unlocked(fd, config)?;
            let name = text(&request, "name")?;
            let store = Store::open(&state)?;
            let (_, profile) = store.load(name)?.with_context(|| format!("no profile {name:?}"))?;
            // a profile whose vault is gone guards nothing: its sealed state cannot be opened
            if unlocked.existing.contains(&profile.vault) {
                unlocked.require(&profile.vault)?;
            }
            Vaulted::open(&state, caller.cred.uid)?.forget(name)?;
            store.delete(name)?;
            json!({"type": "ok"})
        }
        Some("forget") => {
            let unlocked = caller.unlocked(fd, config)?;
            let name = text(&request, "name")?;
            let (_, profile) = Store::open(&state)?.load(name)?.with_context(|| format!("no profile {name:?}"))?;
            unlocked.require(&profile.vault)?;
            let forgotten = Vaulted::open(&state, caller.cred.uid)?.forget(name)?;
            json!({"type": "ok", "forgotten": forgotten})
        }
        other => bail!("unknown request {other:?}"),
    };
    frame::send(stream, &reply, &[])
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

/// Profiles with a login running, by uid and name: two at once would race to save.
static RUNNING: Mutex<Option<HashSet<(u32, String)>>> = Mutex::new(None);

struct Running((u32, String));

impl Running {
    fn claim(uid: u32, name: &str) -> Result<Running> {
        let mut running = RUNNING.lock().unwrap_or_else(|e| e.into_inner());
        let key = (uid, name.to_owned());
        if !running.get_or_insert_with(HashSet::new).insert(key.clone()) {
            bail!("a login for {name:?} is already running");
        }
        Ok(Running(key))
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        let mut running = RUNNING.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(set) = running.as_mut() {
            set.remove(&self.0);
        }
    }
}

/// `get` and `login`: a conversation rather than one reply. After the pidvault check the
/// daemon names the vault holding the profile's key, the client sends the key, and the daemon
/// answers from the sealed state or runs a booth on the client's Wayland connection, relaying
/// its log lines, before sending the result.
fn login(stream: &UnixStream, caller: &Caller, request: &Message, fds: Vec<OwnedFd>, config: &Config, state: &Path) -> Result<()> {
    if fds.is_empty() || fds.len() > 2 {
        bail!("pass a connection to pidvault, and to log in a connection to the Wayland display");
    }
    let mut fds = fds.into_iter();
    let unlocked = caller.unlocked(fds.next(), config)?;
    let wayland = fds.next();
    let name = text(request, "name")?;
    let refresh = request["refresh"].as_bool().unwrap_or(false);
    let log = request["log"].as_bool().unwrap_or(false);
    let (_, profile) = Store::open(state)?.load(name)?.with_context(|| format!("no profile {name:?}"))?;
    unlocked.require(&profile.vault)?;

    frame::send(stream, &json!({"type": "need-key", "vault": profile.vault}), &[])?;
    let (reply, _) = frame::receive(stream)?.context("the client hung up before sending the key")?;
    if reply["type"] != "key" {
        bail!("expected the key, got {:?}", reply["type"]);
    }
    let key = Key::from_base64(text(&reply, "key")?)?;
    caller.still_there()?;

    let sealed = Vaulted::open(state, caller.cred.uid)?;
    let mut current = match sealed.load(name, &key)? {
        Loaded::Fresh => State::default(),
        Loaded::Opened(state) => state,
        Loaded::Unreadable => {
            let note = "the kept state does not open with this key from the vault; starting afresh";
            frame::send(stream, &json!({"type": "log", "text": note}), &[])?;
            State::default()
        }
    };
    if !refresh
        && let (Some(token), Some(expires)) = (&current.token, current.expires)
        && expires > now() + EXPIRY_MARGIN
    {
        return frame::send(stream, &json!({"type": "result", "outcome": "token", "token": token, "expires": expires}), &[]);
    }
    let wayland = wayland.context("no Wayland display to open the login window on")?;
    let _running = Running::claim(caller.cred.uid, name)?;
    let booth_request = booth::request(name, &profile, &current.keep, log);
    let result = match booth::run(&config.booth_socket, &booth_request, wayland, stream, log)? {
        Relayed::CallerGone => return Ok(()),
        Relayed::Done(result) => result,
    };
    let outcome = result["outcome"].as_str().unwrap_or("refused");
    if outcome == "refused" {
        bail!("the booth refused: {}", result["error"].as_str().unwrap_or("no reason given"));
    }
    if !profile.keep_cookies.is_empty() {
        current.keep = result["keep"].as_array().cloned().unwrap_or_default();
    }
    let mut reply = json!({"type": "result", "outcome": outcome, "token": null, "expires": null});
    if let (true, Some(token)) = (outcome == "token", result["token"].as_str()) {
        let expires = booth::expiry(token, result["expires"].as_f64());
        current.token = Some(token.to_owned());
        current.expires = expires;
        reply["token"] = json!(token);
        reply["expires"] = json!(expires);
    }
    sealed.save(name, &key, &current)?;
    frame::send(stream, &reply, &[])
}

/// The caller's own directory under the state root, created on first use, readable by the
/// daemon alone.
fn state_dir(root: &Path, uid: u32) -> Result<PathBuf> {
    let dir = root.join(uid.to_string());
    match DirBuilder::new().mode(0o700).create(&dir) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(e) => return Err(e).with_context(|| format!("cannot create {}", dir.display())),
    }
    let meta = std::fs::symlink_metadata(&dir)?;
    // SAFETY: geteuid has no preconditions
    let me = unsafe { libc::geteuid() };
    if !meta.is_dir() || meta.uid() != me || meta.mode() & 0o077 != 0 {
        bail!("{} is not a private directory of the daemon's", dir.display());
    }
    Ok(dir)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn a_callers_directory_is_created_private_and_reused() {
        let root = tempfile::tempdir().unwrap();
        let dir = state_dir(root.path(), 1000).unwrap();
        assert_eq!(dir, root.path().join("1000"));
        assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777, 0o700);
        assert_eq!(state_dir(root.path(), 1000).unwrap(), dir);
    }

    #[test]
    fn a_symlink_or_open_directory_is_refused() {
        let root = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink("/tmp", root.path().join("1000")).unwrap();
        assert!(state_dir(root.path(), 1000).is_err());
        let open = root.path().join("1001");
        std::fs::create_dir(&open).unwrap();
        std::fs::set_permissions(&open, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(state_dir(root.path(), 1001).is_err());
    }
}

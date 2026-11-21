//! `tokenbooth daemon`: runs as the `tokenbooth` user, serves one request per connection, and
//! knows each caller by the kernel's credentials for the connection.

use std::fs::DirBuilder;
use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result, bail};
use serde_json::json;

use crate::frame;
use crate::procs::{self, PeerCred, Pidfd};

const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone)]
pub struct Config {
    /// Holds one directory per caller's uid.
    pub state: PathBuf,
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
}

fn serve(stream: &UnixStream, config: &Config) -> Result<()> {
    let caller = Caller::of(stream)?;
    stream.set_read_timeout(Some(REQUEST_TIMEOUT))?;
    let Some((request, _fds)) = frame::receive(stream)? else {
        return Ok(());
    };
    match request["type"].as_str() {
        Some("status") => {
            let state = state_dir(&config.state, caller.cred.uid)?;
            caller.still_there()?;
            frame::send(
                stream,
                &json!({"type": "status", "uid": caller.cred.uid, "pid": caller.cred.pid, "state": state}),
                &[],
            )
        }
        other => bail!("unknown request {other:?}"),
    }
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

//! Whether the client may talk to what answers at the daemon's socket. It is about to hand over
//! a profile's key from the vault, so a stand-in must be refused before anything is sent.
//!
//! A connection's peer credentials are those of whoever created the listening socket: the
//! `tokenbooth` account for a daemon that listens itself, or systemd as root under socket
//! activation. A root listener counts only bound in a directory that only root can write,
//! judged by the address the listener was bound to, not the path that was connected to.

use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::path::Path;

use anyhow::{Result, bail};

use crate::procs;

/// Lets a debug build talk to a daemon running as the client's own user, as the tests' daemon
/// does. Release builds ignore it: anything that can edit a shell's startup files could set it.
pub const ALLOW_SAME_UID_ENV: &str = "TOKENBOOTH_INSECURE_ALLOW_SAME_UID_DAEMON";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Dir {
    pub owner: u32,
    pub mode: u32,
}

/// The decision, from facts gathered about the connection.
pub fn judge(peer_uid: u32, own_uid: u32, daemon_uid: Option<u32>, listener_dir: Option<Dir>, allow_same_uid: bool) -> Result<()> {
    if allow_same_uid && peer_uid == own_uid {
        return Ok(());
    }
    if Some(peer_uid) == daemon_uid {
        return Ok(());
    }
    if peer_uid == 0 {
        return match listener_dir {
            Some(dir) if dir.owner == 0 && dir.mode & 0o022 == 0 => Ok(()),
            Some(_) => bail!("the daemon's socket is in a directory others than root can write"),
            None => bail!("the daemon's socket has no address in a directory"),
        };
    }
    if peer_uid == own_uid {
        bail!("the daemon runs as your own user, so it could be any of your processes");
    }
    bail!("the daemon runs as uid {peer_uid}, neither tokenbooth's nor root's");
}

pub fn check(stream: &UnixStream) -> Result<()> {
    let peer = procs::peer_cred(stream)?;
    let address = stream.peer_addr().ok();
    let listener_dir = address
        .as_ref()
        .and_then(|a| a.as_pathname())
        .and_then(Path::parent)
        .and_then(|dir| std::fs::metadata(dir).ok())
        .map(|meta| Dir { owner: meta.uid(), mode: meta.mode() });
    // SAFETY: getuid has no preconditions
    let own = unsafe { libc::getuid() };
    let allow = cfg!(debug_assertions) && std::env::var_os(ALLOW_SAME_UID_ENV).is_some();
    judge(peer.uid, own, procs::uid_of_user("tokenbooth"), listener_dir, allow)
}

#[cfg(test)]
mod tests {
    use super::*;

    const ME: u32 = 1000;
    const DAEMON: Option<u32> = Some(966);
    const ROOTS: Option<Dir> = Some(Dir { owner: 0, mode: 0o40755 });

    #[test]
    fn the_daemon_account_or_root_in_a_root_directory_is_trusted() {
        assert!(judge(966, ME, DAEMON, None, false).is_ok());
        assert!(judge(0, ME, DAEMON, ROOTS, false).is_ok());
    }

    #[test]
    fn anything_else_is_refused() {
        for (peer, dir, complaint) in [
            (ME, ROOTS, "your own user"),
            (1001, ROOTS, "neither tokenbooth's nor root's"),
            (0, Some(Dir { owner: 0, mode: 0o41777 }), "others than root can write"),
            (0, Some(Dir { owner: ME, mode: 0o40700 }), "others than root can write"),
            (0, None, "no address"),
        ] {
            let error = judge(peer, ME, DAEMON, dir, false).unwrap_err().to_string();
            assert!(error.contains(complaint), "{peer} {dir:?}: {error}");
        }
    }

    #[test]
    fn same_uid_only_when_allowed() {
        assert!(judge(ME, ME, DAEMON, None, true).is_ok());
        assert!(judge(1001, ME, DAEMON, None, true).is_err());
    }
}

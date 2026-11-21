//! Whether a caller is in a shell that pidvault has unlocked, asked over a connection to
//! pidvault that the caller opened and passed along (pidvault's "Asking pidvault from another
//! program"). pidvault answers for the process that opened that connection, and refuses once
//! it has exited; the caller must then descend from the shell pidvault names.
//!
//! The connection must lead to pidvault, or a fake could name any of the caller's ancestors.
//! Its peer credentials are those of whoever created the listening socket: the `pidvault`
//! account, or, socket-activated, systemd as root. A root listener counts only bound at
//! pidvault's socket path, a name only root can give it.

use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use serde::Deserialize;

use crate::procs::{self, PeerCred};

const TIMEOUT: Duration = Duration::from_secs(5);
const MAX_REPLY: u64 = 1 << 20;

#[derive(Deserialize)]
struct Reply {
    ok: bool,
    #[serde(default)]
    message: String,
    #[serde(default)]
    vaults: Vec<Vault>,
}

#[derive(Deserialize)]
struct Vault {
    name: String,
    #[serde(default)]
    ephemeral: bool,
    unlocked_by: Option<u32>,
}

/// Where pidvault is, and the account it runs as.
#[derive(Clone)]
pub struct Pidvault {
    pub uid: u32,
    pub socket: std::path::PathBuf,
}

impl Pidvault {
    pub fn new(uid: u32, socket: &Path) -> Pidvault {
        Pidvault { uid, socket: socket.to_owned() }
    }
}

/// The caller's stored vaults that pidvault has unlocked for it, by name, with the shell
/// that unlocked each.
#[derive(Debug, Default)]
pub struct Unlocked(pub BTreeMap<String, u32>);

impl Unlocked {
    pub fn require(&self, vault: &str) -> Result<()> {
        if !self.0.contains_key(vault) {
            bail!("the vault {vault:?} is not unlocked in your shell: run `pidvault unlock {vault}` there");
        }
        Ok(())
    }
}

/// Asks pidvault, over `connection`, about `caller`. The caller must still be checked to be
/// alive afterwards, since its pid can belong to anyone once it has exited.
pub fn ask(connection: OwnedFd, caller: PeerCred, pidvault: &Pidvault) -> Result<Unlocked> {
    let stream = UnixStream::from(connection);
    let peer = procs::peer_cred(&stream).context("the passed fd is not a connected socket")?;
    let address = stream.peer_addr().ok();
    let at_socket = address.as_ref().and_then(|a| a.as_pathname()) == Some(pidvault.socket.as_path());
    if !(peer.uid == pidvault.uid || (peer.uid == 0 && at_socket)) {
        bail!("the passed connection is not to pidvault");
    }
    stream.set_read_timeout(Some(TIMEOUT))?;
    stream.set_write_timeout(Some(TIMEOUT))?;
    (&stream).write_all(b"{\"op\":\"status\"}\n")?;
    stream.shutdown(std::net::Shutdown::Write)?;
    let mut line = String::new();
    BufReader::new((&stream).take(MAX_REPLY)).read_line(&mut line).context("no reply from pidvault")?;
    let reply: Reply = serde_json::from_str(&line).context("malformed reply from pidvault")?;
    if !reply.ok {
        bail!("pidvault cannot tell whether your shell is unlocked: {}", reply.message);
    }
    let mut unlocked = Unlocked::default();
    for vault in reply.vaults {
        let Some(shell) = vault.unlocked_by else { continue };
        if vault.ephemeral {
            continue;
        }
        // pidvault answered for whoever opened the connection; it vouches for this caller
        // only if the caller is in that same unlocked shell
        if procs::descends_from(caller.pid, shell) && procs::real_uid_of(shell) == Some(caller.uid) {
            unlocked.0.insert(vault.name, shell);
        }
    }
    Ok(unlocked)
}

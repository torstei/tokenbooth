use std::os::fd::{FromRawFd, OwnedFd};

use anyhow::{Context, Result, bail};

const FIRST_FD: i32 = 3;

/// The file descriptors systemd passed to this process.
pub fn listen_fds() -> Result<Vec<OwnedFd>> {
    let pid: u32 = std::env::var("LISTEN_PID")
        .context("not started by systemd: no LISTEN_PID")?
        .parse()?;
    if pid != std::process::id() {
        bail!("LISTEN_PID is for another process");
    }
    let count: u8 = std::env::var("LISTEN_FDS")
        .context("no LISTEN_FDS")?
        .parse()
        .context("LISTEN_FDS is not a small count")?;
    let mut fds = Vec::new();
    for fd in FIRST_FD..FIRST_FD + i32::from(count) {
        // SAFETY: fcntl on any integer is sound; it fails with EBADF if fd is not open
        if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
            bail!("LISTEN_FDS names fd {fd}, which is not open");
        }
        // SAFETY: fd is open, and systemd handed it to this process (LISTEN_PID) for it
        // alone; nothing else in the process refers to it
        fds.push(unsafe { OwnedFd::from_raw_fd(fd) });
    }
    for var in ["LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"] {
        // SAFETY: called before the daemon starts any thread, so nothing reads the
        // environment meanwhile
        unsafe { std::env::remove_var(var) };
    }
    Ok(fds)
}

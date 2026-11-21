use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::net::UnixStream;

/// A handle that keeps referring to the same process even after its pid is reused.
pub struct Pidfd(OwnedFd);

impl Pidfd {
    pub fn has_exited(&self) -> bool {
        let mut pfd = libc::pollfd {
            fd: self.0.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        // SAFETY: pfd outlives the call and the count is 1
        let ready = unsafe { libc::poll(&mut pfd, 1, 0) };
        ready != 0
    }
}

/// A pidfd for the process that opened the connection at the other end of `stream`,
/// whoever holds it now. Needs Linux 6.5 or later.
pub fn connected_pidfd(stream: &UnixStream) -> io::Result<Pidfd> {
    let mut fd: libc::c_int = -1;
    let mut len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    // SAFETY: fd and len outlive the call and len is fd's size
    let rc = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERPIDFD,
            (&raw mut fd).cast(),
            &mut len,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    if fd < 0 {
        return Err(io::Error::other("the kernel gave no pidfd for the peer"));
    }
    // SAFETY: a descriptor the kernel just opened for this process, owned by nothing else
    Ok(Pidfd(unsafe { OwnedFd::from_raw_fd(fd) }))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCred {
    pub pid: u32,
    pub uid: u32,
}

/// The process that opened the connection at the other end of `stream`, as the kernel saw it.
pub fn peer_cred(stream: &UnixStream) -> io::Result<PeerCred> {
    let mut cred = libc::ucred {
        pid: 0,
        uid: 0,
        gid: 0,
    };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: cred and len outlive the call and len is cred's size
    let rc = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&raw mut cred).cast(),
            &mut len,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(PeerCred {
        pid: cred.pid as u32,
        uid: cred.uid,
    })
}

/// Keeps other processes of our uid from reading our memory or attaching to us.
pub fn harden() -> io::Result<()> {
    // SAFETY: prctl with integer arguments only
    if unsafe { libc::prctl(libc::PR_SET_DUMPABLE, 0, 0, 0, 0) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

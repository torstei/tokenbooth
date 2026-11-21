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

pub fn parent_of(pid: u32) -> Option<u32> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // comm may itself contain ") ", so split after the last one; the parent follows the state
    let rest = &stat[stat.rfind(')')? + 1..];
    rest.split_whitespace().nth(1)?.parse().ok()
}

pub fn real_uid_of(pid: u32) -> Option<u32> {
    let status = std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    status.lines().find_map(|l| l.strip_prefix("Uid:"))?.split_whitespace().next()?.parse().ok()
}

/// Whether `ancestor` is `pid` or one of its ancestors.
pub fn descends_from(pid: u32, ancestor: u32) -> bool {
    let mut current = pid;
    for _ in 0..1024 {
        if current == ancestor {
            return true;
        }
        match parent_of(current) {
            Some(parent) if parent != 0 && parent != current => current = parent,
            _ => return false,
        }
    }
    false
}

/// The uid of the account `name`, if it has a passwd entry.
pub fn uid_of_user(name: &str) -> Option<u32> {
    let name = std::ffi::CString::new(name).ok()?;
    let mut buf = vec![0 as libc::c_char; 16 * 1024];
    // SAFETY: passwd is plain data, for which all zeros is a valid value
    let mut pwd: libc::passwd = unsafe { std::mem::zeroed() };
    let mut result: *mut libc::passwd = std::ptr::null_mut();
    // SAFETY: name, pwd, buf and result outlive the call and buf.len() is buf's size; pwd's
    // string pointers into buf are never read
    let rc = unsafe { libc::getpwnam_r(name.as_ptr(), &mut pwd, buf.as_mut_ptr(), buf.len(), &mut result) };
    if rc != 0 || result.is_null() {
        return None;
    }
    Some(pwd.pw_uid)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn this_process_descends_from_its_parent_and_itself_but_not_a_child() {
        let me = std::process::id();
        let parent = parent_of(me).unwrap();
        assert!(descends_from(me, me));
        assert!(descends_from(me, parent));
        let mut child = std::process::Command::new("sleep").arg("5").spawn().unwrap();
        assert!(!descends_from(me, child.id()));
        assert!(descends_from(child.id(), me));
        child.kill().unwrap();
        child.wait().unwrap();
    }

    #[test]
    fn root_is_uid_0() {
        assert_eq!(uid_of_user("root"), Some(0));
        assert_eq!(uid_of_user("no-such-user-here"), None);
    }
}

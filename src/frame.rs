//! Frames as in docs/booth-protocol.md: a 4-byte big-endian length, then that many bytes of
//! a UTF-8 JSON object with a string `type`; file descriptors travel with a frame's first bytes.

use std::io::{self, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::net::UnixStream;

use anyhow::{Context, Result, bail};
use serde_json::{Map, Value};

pub const MAX_FRAME: usize = 1 << 20;
pub const MAX_FDS: usize = 4;

pub type Message = Map<String, Value>;

pub fn send(stream: &UnixStream, message: &Value, fds: &[RawFd]) -> Result<()> {
    let body = serde_json::to_vec(message)?;
    if body.len() > MAX_FRAME {
        bail!("frame of {} bytes exceeds {MAX_FRAME}", body.len());
    }
    if fds.len() > MAX_FDS {
        bail!("more than {MAX_FDS} file descriptors in one frame");
    }
    let mut data = (body.len() as u32).to_be_bytes().to_vec();
    data.extend_from_slice(&body);
    let sent = if fds.is_empty() { 0 } else { send_with_fds(stream, &data, fds)? };
    (&*stream).write_all(&data[sent..])?;
    Ok(())
}

fn send_with_fds(stream: &UnixStream, data: &[u8], fds: &[RawFd]) -> io::Result<usize> {
    let fd_bytes = std::mem::size_of_val(fds);
    // SAFETY: CMSG_SPACE computes a size from an integer
    let space = unsafe { libc::CMSG_SPACE(fd_bytes as u32) } as usize;
    let mut control = vec![0u8; space];
    let mut iov = libc::iovec {
        iov_base: data.as_ptr() as *mut libc::c_void,
        iov_len: data.len(),
    };
    // SAFETY: an all-zero msghdr is a valid empty one
    let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
    msg.msg_iov = &mut iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control.as_mut_ptr().cast();
    msg.msg_controllen = space;
    // SAFETY: msg points at control, which has room for one header and fds.len() ints;
    // CMSG_FIRSTHDR is therefore non-null and CMSG_DATA inside control
    unsafe {
        let cmsg = libc::CMSG_FIRSTHDR(&msg);
        (*cmsg).cmsg_level = libc::SOL_SOCKET;
        (*cmsg).cmsg_type = libc::SCM_RIGHTS;
        (*cmsg).cmsg_len = libc::CMSG_LEN(fd_bytes as u32) as usize;
        std::ptr::copy_nonoverlapping(fds.as_ptr(), libc::CMSG_DATA(cmsg).cast(), fds.len());
    }
    // SAFETY: msg and everything it points to outlive the call
    let sent = unsafe { libc::sendmsg(stream.as_raw_fd(), &msg, libc::MSG_NOSIGNAL) };
    if sent < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(sent as usize)
}

/// Reads up to `buf.len()` bytes, collecting any descriptors passed with them.
fn recv_with_fds(stream: &UnixStream, buf: &mut [u8], fds: &mut Vec<OwnedFd>) -> Result<usize> {
    // SAFETY: CMSG_SPACE computes a size from an integer
    let space = unsafe { libc::CMSG_SPACE((MAX_FDS * std::mem::size_of::<RawFd>()) as u32) } as usize;
    let mut control = vec![0u8; space];
    let mut iov = libc::iovec {
        iov_base: buf.as_mut_ptr().cast(),
        iov_len: buf.len(),
    };
    // SAFETY: an all-zero msghdr is a valid empty one
    let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
    msg.msg_iov = &mut iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control.as_mut_ptr().cast();
    msg.msg_controllen = space;
    // SAFETY: msg points at buf and control, both writable for the lengths given
    let read = unsafe { libc::recvmsg(stream.as_raw_fd(), &mut msg, libc::MSG_CMSG_CLOEXEC) };
    if read < 0 {
        return Err(io::Error::last_os_error().into());
    }
    // SAFETY: the kernel filled control and set msg_controllen; the CMSG_* walk stays
    // within it, and each SCM_RIGHTS payload holds whole ints the kernel just opened for us
    unsafe {
        let mut cmsg = libc::CMSG_FIRSTHDR(&msg);
        while !cmsg.is_null() {
            if (*cmsg).cmsg_level == libc::SOL_SOCKET && (*cmsg).cmsg_type == libc::SCM_RIGHTS {
                let payload = (*cmsg).cmsg_len as usize - libc::CMSG_LEN(0) as usize;
                let data: *const RawFd = libc::CMSG_DATA(cmsg).cast();
                for i in 0..payload / std::mem::size_of::<RawFd>() {
                    fds.push(OwnedFd::from_raw_fd(std::ptr::read_unaligned(data.add(i))));
                }
            }
            cmsg = libc::CMSG_NXTHDR(&msg, cmsg);
        }
    }
    if msg.msg_flags & libc::MSG_CTRUNC != 0 {
        bail!("more than {MAX_FDS} file descriptors passed");
    }
    Ok(read as usize)
}

fn read_exactly(stream: &UnixStream, size: usize, fds: &mut Vec<OwnedFd>) -> Result<Option<Vec<u8>>> {
    let mut data = vec![0u8; size];
    let mut filled = 0;
    while filled < size {
        let read = recv_with_fds(stream, &mut data[filled..], fds)?;
        if read == 0 {
            if filled == 0 {
                return Ok(None);
            }
            bail!("connection closed inside a frame");
        }
        filled += read;
    }
    Ok(Some(data))
}

/// The next frame and the descriptors passed with it, or None when the peer closed
/// between frames.
pub fn receive(stream: &UnixStream) -> Result<Option<(Message, Vec<OwnedFd>)>> {
    let mut fds = Vec::new();
    let Some(header) = read_exactly(stream, 4, &mut fds)? else {
        return Ok(None);
    };
    let size = u32::from_be_bytes(header.try_into().expect("four bytes")) as usize;
    if size > MAX_FRAME {
        bail!("frame of {size} bytes exceeds {MAX_FRAME}");
    }
    let body = read_exactly(stream, size, &mut fds)?.context("connection closed inside a frame")?;
    let Value::Object(message) = serde_json::from_slice(&body).context("frame is not JSON")? else {
        bail!("frame is not an object");
    };
    if !message.get("type").is_some_and(Value::is_string) {
        bail!("frame has no type");
    }
    Ok(Some((message, fds)))
}

/// Reads a whole frame's worth from a reader with no descriptors, such as a test's buffer.
#[cfg(test)]
fn decode(mut reader: impl io::Read) -> Result<Message> {
    let mut header = [0u8; 4];
    reader.read_exact(&mut header)?;
    let mut body = vec![0u8; u32::from_be_bytes(header) as usize];
    reader.read_exact(&mut body)?;
    match serde_json::from_slice(&body)? {
        Value::Object(message) => Ok(message),
        _ => bail!("not an object"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::Read;

    #[test]
    fn a_frame_carries_a_passed_fd() {
        let (a, b) = UnixStream::pair().unwrap();
        let (r, w) = UnixStream::pair().unwrap();
        send(&a, &json!({"type": "request", "n": 1}), &[w.as_raw_fd()]).unwrap();
        drop(w);
        let (message, fds) = receive(&b).unwrap().unwrap();
        assert_eq!(Value::Object(message), json!({"type": "request", "n": 1}));
        assert_eq!(fds.len(), 1);
        let mut passed = UnixStream::from(fds.into_iter().next().unwrap());
        passed.write_all(b"through").unwrap();
        drop(passed);
        let mut got = String::new();
        (&r).read_to_string(&mut got).unwrap();
        assert_eq!(got, "through");
    }

    #[test]
    fn a_clean_close_between_frames_is_none() {
        let (a, b) = UnixStream::pair().unwrap();
        drop(a);
        assert!(receive(&b).unwrap().is_none());
    }

    #[test]
    fn bad_frames_are_refused() {
        for (data, complaint) in [
            (((MAX_FRAME + 1) as u32).to_be_bytes().to_vec(), "exceeds"),
            ([&10u32.to_be_bytes()[..], b"{}"].concat(), "inside a frame"),
            ([&2u32.to_be_bytes()[..], b"[]"].concat(), "not an object"),
            ([&2u32.to_be_bytes()[..], b"{}"].concat(), "no type"),
        ] {
            let (a, b) = UnixStream::pair().unwrap();
            (&a).write_all(&data).unwrap();
            drop(a);
            let error = receive(&b).unwrap_err().to_string();
            assert!(error.contains(complaint), "{error}");
        }
    }

    #[test]
    fn the_encoding_matches_the_python_booth_protocol() {
        let (a, b) = UnixStream::pair().unwrap();
        send(&a, &json!({"type": "log", "text": "x"}), &[]).unwrap();
        drop(a);
        let message = decode(&b).unwrap();
        assert_eq!(message["text"], "x");
    }
}

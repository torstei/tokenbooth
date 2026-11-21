//! The binary as daemon and as client, on a socket of the test's own.

use std::path::Path;
use std::process::{Child, Command, Output};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_tokenbooth");

struct Daemon(Child);

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn start_daemon(dir: &Path) -> Daemon {
    let socket = dir.join("sock");
    let daemon = Command::new(BIN)
        .args(["daemon", "--listen"])
        .arg(&socket)
        .arg("--state")
        .arg(dir.join("state"))
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !socket.exists() {
        assert!(Instant::now() < deadline, "the daemon never listened");
        std::thread::sleep(Duration::from_millis(20));
    }
    Daemon(daemon)
}

fn client(dir: &Path, args: &[&str]) -> Output {
    Command::new(BIN).env("TOKENBOOTH_SOCKET", dir.join("sock")).args(args).output().unwrap()
}

#[test]
fn status_reports_the_caller_the_kernel_saw() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("state")).unwrap();
    let _daemon = start_daemon(dir.path());
    let output = client(dir.path(), &["status"]);
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    let text = String::from_utf8(output.stdout).unwrap();
    // SAFETY: getuid has no preconditions
    let uid = unsafe { libc::getuid() };
    assert!(text.contains(&format!("uid   {uid}\n")), "{text}");
    assert!(text.contains(&format!("state {}", dir.path().join("state").join(uid.to_string()).display())), "{text}");
    assert!(dir.path().join("state").join(uid.to_string()).is_dir());
}

#[test]
fn an_unknown_request_gets_a_reason() {
    use std::io::{Read, Write};
    use std::os::unix::net::UnixStream;
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("state")).unwrap();
    let _daemon = start_daemon(dir.path());
    let mut stream = UnixStream::connect(dir.path().join("sock")).unwrap();
    let body = br#"{"type":"steal"}"#;
    stream.write_all(&(body.len() as u32).to_be_bytes()).unwrap();
    stream.write_all(body).unwrap();
    let mut reply = Vec::new();
    stream.read_to_end(&mut reply).unwrap();
    assert!(String::from_utf8_lossy(&reply[4..]).contains("unknown request"));
}

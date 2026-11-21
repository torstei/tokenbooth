//! The binary as daemon and as client, on sockets of the test's own, with a stand-in for
//! pidvault whose answer each test chooses. The client runs as a child of the test process, so
//! an answer naming the test process as the unlocked shell vouches for it.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixListener;
use std::path::Path;
use std::process::{Child, Command, Output, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_tokenbooth");

const GOPRO: &str = "start_url = \"https://gopro.com/login\"\ncookie = \"gp_access_token\"\n";

struct Setup {
    dir: tempfile::TempDir,
    daemon: Child,
    answer: Arc<Mutex<String>>,
}

impl Drop for Setup {
    fn drop(&mut self) {
        let _ = self.daemon.kill();
        let _ = self.daemon.wait();
    }
}

fn wait_for(path: &Path) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !path.exists() {
        assert!(Instant::now() < deadline, "nothing listened at {}", path.display());
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn unlocked_by(pid: u32) -> String {
    format!(r#"{{"ok":true,"message":"","vaults":[{{"name":"default","ephemeral":false,"unlocked_by":{pid},"sessions":1}},{{"name":"work","ephemeral":false,"unlocked_by":null,"sessions":0}}]}}"#)
}

fn me() -> u32 {
    std::process::id()
}

fn setup_as(pidvault_user: &str) -> Setup {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("state")).unwrap();
    let answer = Arc::new(Mutex::new(unlocked_by(me())));
    let listener = UnixListener::bind(dir.path().join("pidvault.sock")).unwrap();
    let shared = Arc::clone(&answer);
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let mut line = String::new();
            BufReader::new(&stream).read_line(&mut line).unwrap();
            assert_eq!(line, "{\"op\":\"status\"}\n");
            let reply = format!("{}\n", shared.lock().unwrap());
            let _ = (&stream).write_all(reply.as_bytes());
        }
    });
    let daemon = Command::new(BIN)
        .args(["daemon", "--pidvault-user", pidvault_user, "--listen"])
        .arg(dir.path().join("sock"))
        .arg("--state")
        .arg(dir.path().join("state"))
        .spawn()
        .unwrap();
    wait_for(&dir.path().join("sock"));
    Setup { dir, daemon, answer }
}

fn setup() -> Setup {
    setup_as(&std::env::var("USER").expect("USER names the test's account"))
}

impl Setup {
    fn run(&self, args: &[&str], stdin: &str) -> Output {
        let mut child = Command::new(BIN)
            .env("TOKENBOOTH_SOCKET", self.dir.path().join("sock"))
            .env("PIDVAULT_SOCKET", self.dir.path().join("pidvault.sock"))
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        child.stdin.take().unwrap().write_all(stdin.as_bytes()).unwrap();
        child.wait_with_output().unwrap()
    }

    fn ok(&self, args: &[&str], stdin: &str) -> String {
        let output = self.run(args, stdin);
        assert!(output.status.success(), "{args:?}: {}", String::from_utf8_lossy(&output.stderr));
        String::from_utf8(output.stdout).unwrap()
    }

    fn refused(&self, args: &[&str], stdin: &str) -> String {
        let output = self.run(args, stdin);
        assert!(!output.status.success(), "{args:?} was not refused");
        String::from_utf8(output.stderr).unwrap()
    }

    fn answer(&self, answer: String) {
        *self.answer.lock().unwrap() = answer;
    }
}

#[test]
fn status_reports_the_caller_the_kernel_saw_and_its_unlocked_vaults() {
    let s = setup();
    let text = s.ok(&["status"], "");
    // SAFETY: getuid has no preconditions
    let uid = unsafe { libc::getuid() };
    assert!(text.contains(&format!("uid      {uid}\n")), "{text}");
    assert!(text.contains("unlocked default\n"), "{text}");
    assert!(s.dir.path().join("state").join(uid.to_string()).is_dir());
}

#[test]
fn profiles_are_kept_for_an_unlocked_shell() {
    let s = setup();
    s.ok(&["profile", "set", "gopro", "-"], GOPRO);
    assert_eq!(s.ok(&["profile", "list"], ""), "gopro\t(vault default)\n");
    assert_eq!(s.ok(&["profile", "show", "gopro"], ""), GOPRO);
    s.ok(&["profile", "delete", "gopro"], "");
    assert_eq!(s.ok(&["profile", "list"], ""), "");
}

#[test]
fn a_profile_in_a_locked_vault_is_neither_listed_nor_set() {
    let s = setup();
    let work = format!("{GOPRO}vault = \"work\"\n");
    assert!(s.refused(&["profile", "set", "w", "-"], &work).contains("\"work\" is not unlocked"));
    s.ok(&["profile", "set", "gopro", "-"], GOPRO);
    assert!(s.refused(&["profile", "set", "gopro", "-"], &work).contains("\"work\" is not unlocked"));
}

#[test]
fn a_locked_shell_is_refused() {
    let s = setup();
    s.ok(&["profile", "set", "gopro", "-"], GOPRO);
    s.answer(r#"{"ok":true,"message":"","vaults":[{"name":"default","ephemeral":false,"unlocked_by":null,"sessions":0}]}"#.into());
    assert!(s.refused(&["profile", "show", "gopro"], "").contains("not unlocked"));
    assert_eq!(s.ok(&["profile", "list"], ""), "");
}

#[test]
fn pidvault_that_cannot_tell_is_a_refusal() {
    let s = setup();
    s.answer(r#"{"ok":false,"message":"the process that opened this connection has exited","vaults":[]}"#.into());
    let error = s.refused(&["profile", "list"], "");
    assert!(error.contains("cannot tell") && error.contains("has exited"), "{error}");
}

#[test]
fn a_shell_the_caller_does_not_descend_from_vouches_for_nothing() {
    let s = setup();
    let mut stranger = Command::new("sleep").arg("30").spawn().unwrap();
    s.answer(unlocked_by(stranger.id()));
    assert!(s.refused(&["profile", "set", "gopro", "-"], GOPRO).contains("not unlocked"));
    stranger.kill().unwrap();
    stranger.wait().unwrap();
}

#[test]
fn a_connection_to_anyone_but_pidvault_is_refused() {
    let s = setup_as("root");
    assert!(s.refused(&["profile", "list"], "").contains("not to pidvault"));
}

#[test]
fn a_profile_request_without_a_pidvault_connection_is_refused() {
    use std::os::unix::net::UnixStream;
    let s = setup();
    let stream = UnixStream::connect(s.dir.path().join("sock")).unwrap();
    let body = br#"{"type":"profile-list"}"#;
    (&stream).write_all(&(body.len() as u32).to_be_bytes()).unwrap();
    (&stream).write_all(body).unwrap();
    let mut reply = Vec::new();
    std::io::Read::read_to_end(&mut &stream, &mut reply).unwrap();
    assert!(String::from_utf8_lossy(&reply).contains("a connection to pidvault"));
}

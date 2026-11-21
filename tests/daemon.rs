//! The binary as daemon and as client, on sockets of the test's own, with a stand-in for
//! pidvault whose answer each test chooses. The client runs as a child of the test process, so
//! an answer naming the test process as the unlocked shell vouches for it.

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
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
    /// Frames the fake booth sends back to each request.
    booth_says: Arc<Mutex<Vec<serde_json::Value>>>,
    /// Requests the fake booth received.
    booth_heard: Arc<Mutex<Vec<serde_json::Value>>>,
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
    std::fs::create_dir_all(dir.path().join("mount/stored/default")).unwrap();
    let compositor = UnixListener::bind(dir.path().join("wayland-0")).unwrap();
    std::thread::spawn(move || for _ in compositor.incoming() {});
    let booth_says = Arc::new(Mutex::new(Vec::new()));
    let booth_heard = Arc::new(Mutex::new(Vec::new()));
    let booth = UnixListener::bind(dir.path().join("booth.sock")).unwrap();
    let (says, heard) = (Arc::clone(&booth_says), Arc::clone(&booth_heard));
    std::thread::spawn(move || {
        for stream in booth.incoming().flatten() {
            if let Some(request) = read_frame(&stream) {
                heard.lock().unwrap().push(request);
                for frame in says.lock().unwrap().iter() {
                    write_frame(&stream, frame);
                }
            }
        }
    });
    let daemon = Command::new(BIN)
        .args(["daemon", "--pidvault-user", pidvault_user, "--listen"])
        .arg(dir.path().join("sock"))
        .arg("--state")
        .arg(dir.path().join("state"))
        .arg("--booth-socket")
        .arg(dir.path().join("booth.sock"))
        .spawn()
        .unwrap();
    wait_for(&dir.path().join("sock"));
    Setup { dir, daemon, answer, booth_says, booth_heard }
}

fn setup() -> Setup {
    setup_as(&std::env::var("USER").expect("USER names the test's account"))
}

impl Setup {
    fn run(&self, args: &[&str], stdin: &str) -> Output {
        let mut child = Command::new(BIN)
            .env("TOKENBOOTH_SOCKET", self.dir.path().join("sock"))
            .env("PIDVAULT_SOCKET", self.dir.path().join("pidvault.sock"))
            .env("PIDVAULT_MOUNT", self.dir.path().join("mount"))
            .env("XDG_RUNTIME_DIR", self.dir.path())
            .env("WAYLAND_DISPLAY", "wayland-0")
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

    fn booth_says(&self, frames: Vec<serde_json::Value>) {
        *self.booth_says.lock().unwrap() = frames;
    }

    fn booth_heard(&self) -> Vec<serde_json::Value> {
        self.booth_heard.lock().unwrap().clone()
    }

    fn uid_dir(&self) -> std::path::PathBuf {
        // SAFETY: getuid has no preconditions
        self.dir.path().join("state").join(unsafe { libc::getuid() }.to_string())
    }
}

/// One frame read plainly: a descriptor passed with it is discarded by the kernel.
fn read_frame(mut stream: &UnixStream) -> Option<serde_json::Value> {
    let mut header = [0u8; 4];
    stream.read_exact(&mut header).ok()?;
    let mut body = vec![0u8; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut body).ok()?;
    serde_json::from_slice(&body).ok()
}

fn write_frame(mut stream: &UnixStream, frame: &serde_json::Value) {
    let body = serde_json::to_vec(frame).unwrap();
    let _ = stream.write_all(&(body.len() as u32).to_be_bytes());
    let _ = stream.write_all(&body);
}

fn in_an_hour() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64() + 3600.0
}

const KEPT: &str = "start_url = \"https://gopro.com/login\"\ncookie = \"gp_access_token\"\nvia = [\"google\"]\nkeep_cookies = [\"*@google.com\"]\n";

fn sid() -> serde_json::Value {
    serde_json::json!({"name": "SID", "domain": ".google.com", "path": "/", "expires": null, "raw": "e30="})
}

fn token_result(token: &str) -> serde_json::Value {
    serde_json::json!({"type": "result", "outcome": "token", "token": token, "expires": in_an_hour(), "keep": [sid()]})
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

#[test]
fn get_runs_a_booth_once_then_answers_from_the_sealed_state() {
    let s = setup();
    s.ok(&["profile", "set", "gopro", "-"], KEPT);
    s.booth_says(vec![serde_json::json!({"type": "log", "text": "navigate https://gopro.com/login"}), token_result("tok-1")]);
    let output = s.run(&["get", "-v", "gopro"], "");
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "tok-1\n");
    assert!(String::from_utf8_lossy(&output.stderr).contains("navigate https://gopro.com/login"));
    assert_eq!(s.ok(&["get", "gopro"], ""), "tok-1\n");
    let heard = s.booth_heard();
    assert_eq!(heard.len(), 1, "the second get should not have opened a booth");
    assert_eq!(heard[0]["profile"]["name"], "gopro");
    assert_eq!(heard[0]["profile"]["cookie_domain"], "gopro.com");
    assert_eq!(heard[0]["keep"], serde_json::json!([]));

    use std::os::unix::fs::PermissionsExt;
    let key = s.dir.path().join("mount/stored/default/tokenbooth/gopro.key");
    assert_eq!(std::fs::metadata(&key).unwrap().permissions().mode() & 0o777, 0o600);
    let blob = std::fs::read(s.uid_dir().join("sealed/gopro")).unwrap();
    assert!(!String::from_utf8_lossy(&blob).contains("tok-1"));
    assert!(!String::from_utf8_lossy(&blob).contains("SID"));
}

#[test]
fn login_seeds_the_booth_with_what_the_last_run_kept() {
    let s = setup();
    s.ok(&["profile", "set", "gopro", "-"], KEPT);
    s.booth_says(vec![token_result("tok-1")]);
    s.ok(&["get", "gopro"], "");
    s.booth_says(vec![token_result("tok-2")]);
    assert_eq!(s.ok(&["login", "gopro"], ""), "tok-2\n");
    let heard = s.booth_heard();
    assert_eq!(heard[1]["keep"], serde_json::json!([sid()]));
    assert_eq!(s.ok(&["get", "gopro"], ""), "tok-2\n");
}

#[test]
fn forget_and_a_replaced_key_both_start_afresh() {
    let s = setup();
    s.ok(&["profile", "set", "gopro", "-"], KEPT);
    s.booth_says(vec![token_result("tok-1")]);
    s.ok(&["get", "gopro"], "");
    s.ok(&["forget", "gopro"], "");
    s.ok(&["get", "gopro"], "");
    assert_eq!(s.booth_heard()[1]["keep"], serde_json::json!([]));

    s.booth_says(vec![token_result("tok-2")]);
    s.ok(&["login", "gopro"], "");
    assert_eq!(s.booth_heard()[2]["keep"], serde_json::json!([sid()]));
    std::fs::write(s.dir.path().join("mount/stored/default/tokenbooth/gopro.key"), "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n").unwrap();
    let output = s.run(&["get", "gopro"], "");
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("starting afresh"));
    assert_eq!(s.booth_heard()[3]["keep"], serde_json::json!([]));
}

#[test]
fn each_outcome_has_its_exit_status() {
    let s = setup();
    s.ok(&["profile", "set", "gopro", "-"], KEPT);
    for (result, status, says) in [
        (serde_json::json!({"type": "result", "outcome": "cancelled", "token": null, "expires": null, "keep": []}), 1, "closed without a token"),
        (serde_json::json!({"type": "result", "outcome": "timed_out", "token": null, "expires": null, "keep": []}), 3, "timed out"),
        (serde_json::json!({"type": "result", "outcome": "refused", "token": null, "expires": null, "keep": [], "error": "no Wayland display"}), 2, "the booth refused: no Wayland display"),
    ] {
        s.booth_says(vec![result]);
        let output = s.run(&["get", "gopro"], "");
        assert_eq!(output.status.code(), Some(status));
        assert!(String::from_utf8_lossy(&output.stderr).contains(says), "{}", String::from_utf8_lossy(&output.stderr));
    }
}

#[test]
fn without_a_display_only_a_cached_token_is_answered() {
    let s = setup();
    s.ok(&["profile", "set", "gopro", "-"], KEPT);
    let headless = |args: &[&str]| {
        Command::new(BIN)
            .env("TOKENBOOTH_SOCKET", s.dir.path().join("sock"))
            .env("PIDVAULT_SOCKET", s.dir.path().join("pidvault.sock"))
            .env("PIDVAULT_MOUNT", s.dir.path().join("mount"))
            .env_remove("WAYLAND_DISPLAY")
            .args(args)
            .output()
            .unwrap()
    };
    let output = headless(&["get", "gopro"]);
    assert!(String::from_utf8_lossy(&output.stderr).contains("no Wayland display"));
    s.booth_says(vec![token_result("tok-1")]);
    s.ok(&["get", "gopro"], "");
    assert_eq!(String::from_utf8_lossy(&headless(&["get", "gopro"]).stdout), "tok-1\n");
}

#[test]
fn a_profile_whose_vault_is_gone_can_be_deleted_but_one_in_a_locked_vault_cannot() {
    let s = setup();
    s.answer(r#"{"ok":true,"message":"","vaults":[{"name":"work","ephemeral":false,"unlocked_by":PID,"sessions":1}]}"#.replace("PID", &me().to_string()));
    s.ok(&["profile", "set", "w", "-"], &format!("{GOPRO}vault = \"work\"\n"));
    s.answer(r#"{"ok":true,"message":"","vaults":[{"name":"work","ephemeral":false,"unlocked_by":null,"sessions":0}]}"#.into());
    assert!(s.refused(&["profile", "delete", "w"], "").contains("not unlocked"));
    s.answer(r#"{"ok":true,"message":"","vaults":[]}"#.into());
    s.ok(&["profile", "delete", "w"], "");
}

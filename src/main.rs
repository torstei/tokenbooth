//! tokenbooth: logins in a locked-down browser, for shells that pidvault has unlocked.

mod booth;
mod daemon;
mod frame;
mod pidvault;
mod procs;
mod profile;
mod sealed;
mod systemd;

use std::fs::{DirBuilder, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use serde_json::{Value, json};

const SOCKET: &str = "/run/tokenbooth/sock";
const PIDVAULT_SOCKET: &str = "/run/pidvault.sock";
const PIDVAULT_MOUNT: &str = "/run/pidvault";
const BOOTH_SOCKET: &str = "/run/tokenbooth/booth.sock";
const STATE: &str = "/var/lib/tokenbooth";

const EXIT_CANCELLED: u8 = 1;
const EXIT_ERROR: u8 = 2;
const EXIT_TIMEOUT: u8 = 3;

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    /// The daemon's socket
    #[arg(long, env = "TOKENBOOTH_SOCKET", default_value = SOCKET, global = true)]
    socket: PathBuf,
    /// pidvault's control socket, which the daemon asks whether this shell is unlocked
    #[arg(long, env = "PIDVAULT_SOCKET", default_value = PIDVAULT_SOCKET, global = true)]
    pidvault_socket: PathBuf,
    /// Where pidvault shows unlocked vaults; each profile's key is kept in its vault there
    #[arg(long, env = "PIDVAULT_MOUNT", default_value = PIDVAULT_MOUNT, global = true)]
    pidvault_mount: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(clap::Args)]
struct LoginArgs {
    /// The profile
    name: String,
    /// Log navigations and cookie names (never values) to stderr
    #[arg(short, long)]
    verbose: bool,
    /// Print {"token": ..., "expires": ...} instead of the bare token
    #[arg(long, conflicts_with = "to_vault")]
    json: bool,
    /// Write the token to tokens/NAME in the profile's vault instead of printing it, where only
    /// processes the vault is unlocked for can read it
    #[arg(long)]
    to_vault: bool,
}

#[derive(Subcommand)]
enum Command {
    /// Print the profile's token: the cached one while it is valid, else from the login window
    Get(LoginArgs),
    /// Open the login window and print the token it captures
    Login(LoginArgs),
    /// Drop the profile's cached token and kept cookies
    Forget { name: String },
    /// Show who the daemon takes you to be, and which of your vaults it sees unlocked here
    Status,
    /// Manage profiles; each needs its vault unlocked in this shell
    #[command(subcommand)]
    Profile(ProfileCommand),
    /// Run the daemon (started by systemd as the tokenbooth user)
    Daemon {
        /// Listen here instead of on the socket systemd passes
        #[arg(long, hide = true)]
        listen: Option<PathBuf>,
        /// Keep each caller's state under this directory
        #[arg(long, default_value = STATE)]
        state: PathBuf,
        /// The account pidvault runs as
        #[arg(long, default_value = "pidvault", hide = true)]
        pidvault_user: String,
        /// Where booths are started
        #[arg(long, default_value = BOOTH_SOCKET, hide = true)]
        booth_socket: PathBuf,
    },
}

#[derive(Subcommand)]
enum ProfileCommand {
    /// List the profiles whose vault is unlocked here
    List,
    /// Print a profile
    Show { name: String },
    /// Create or replace a profile from a TOML file (`-` for stdin)
    Set { name: String, file: PathBuf },
    /// Delete a profile
    Delete { name: String },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let outcome = match &cli.command {
        Command::Get(args) => login(&cli, args, false),
        Command::Login(args) => login(&cli, args, true),
        Command::Forget { name } => forget(&cli, name).map(|()| ExitCode::SUCCESS),
        Command::Status => status(&cli).map(|()| ExitCode::SUCCESS),
        Command::Profile(command) => profile(&cli, command).map(|()| ExitCode::SUCCESS),
        Command::Daemon { listen, state, pidvault_user, booth_socket } => {
            run_daemon(listen.clone(), state.clone(), pidvault_user, &cli.pidvault_socket, booth_socket.clone())
                .map(|()| ExitCode::SUCCESS)
        }
    };
    outcome.unwrap_or_else(|e| {
        eprintln!("tokenbooth: {e:#}");
        ExitCode::from(EXIT_ERROR)
    })
}

fn run_daemon(
    listen: Option<PathBuf>,
    state: PathBuf,
    pidvault_user: &str,
    pidvault_socket: &Path,
    booth_socket: PathBuf,
) -> Result<()> {
    let pidvault_uid = procs::uid_of_user(pidvault_user).with_context(|| format!("no account {pidvault_user:?}"))?;
    let listener = match listen {
        Some(path) => UnixListener::bind(&path).with_context(|| format!("cannot listen on {}", path.display()))?,
        None => {
            let mut fds = systemd::listen_fds()?;
            if fds.len() != 1 {
                bail!("expected one socket from systemd, got {}", fds.len());
            }
            UnixListener::from(fds.remove(0))
        }
    };
    let pidvault = pidvault::Pidvault::new(pidvault_uid, pidvault_socket);
    daemon::run(listener, daemon::Config { state, pidvault, booth_socket })
}

/// Sends `request` with, when `pidvault` is set, a fresh connection to pidvault: the daemon
/// asks over it, and pidvault answers for this process, which opened it.
fn call(cli: &Cli, request: &Value, pidvault: bool) -> Result<frame::Message> {
    let socket = &cli.socket;
    let stream = UnixStream::connect(socket).with_context(|| format!("cannot reach the daemon at {}", socket.display()))?;
    let vault = if pidvault { Some(connect_pidvault(&cli.pidvault_socket)?) } else { None };
    let fds: Vec<_> = vault.iter().map(AsRawFd::as_raw_fd).collect();
    frame::send(&stream, request, &fds)?;
    drop(vault);
    let (reply, _) = frame::receive(&stream)?.context("the daemon closed the connection without a reply")?;
    if reply["type"] == "error" {
        bail!("{}", reply["message"].as_str().unwrap_or("refused"));
    }
    Ok(reply)
}

fn connect_pidvault(path: &Path) -> Result<UnixStream> {
    UnixStream::connect(path).with_context(|| format!("cannot reach pidvault at {}", path.display()))
}

fn status(cli: &Cli) -> Result<()> {
    let pidvault = cli.pidvault_socket.exists();
    let reply = call(cli, &json!({"type": "status"}), pidvault)?;
    println!("uid      {}", reply["uid"]);
    println!("pid      {} (this command)", reply["pid"]);
    println!("state    {}", reply["state"].as_str().unwrap_or("?"));
    match reply["unlocked"].as_array() {
        Some(vaults) if vaults.is_empty() => println!("unlocked none in this shell"),
        Some(vaults) => {
            let names: Vec<_> = vaults.iter().filter_map(Value::as_str).collect();
            println!("unlocked {}", names.join(" "));
        }
        None => println!("unlocked (pidvault not reachable)"),
    }
    Ok(())
}

fn profile(cli: &Cli, command: &ProfileCommand) -> Result<()> {
    match command {
        ProfileCommand::List => {
            let reply = call(cli, &json!({"type": "profile-list"}), true)?;
            for entry in reply["profiles"].as_array().into_iter().flatten() {
                println!("{}\t(vault {})", entry["name"].as_str().unwrap_or("?"), entry["vault"].as_str().unwrap_or("?"));
            }
        }
        ProfileCommand::Show { name } => {
            let reply = call(cli, &json!({"type": "profile-show", "name": name}), true)?;
            print!("{}", reply["toml"].as_str().unwrap_or_default());
        }
        ProfileCommand::Set { name, file } => {
            let mut toml = String::new();
            if file.as_os_str() == "-" {
                std::io::stdin().read_to_string(&mut toml)?;
            } else {
                toml = std::fs::read_to_string(file).with_context(|| format!("cannot read {}", file.display()))?;
            }
            call(cli, &json!({"type": "profile-set", "name": name, "toml": toml}), true)?;
        }
        ProfileCommand::Delete { name } => {
            call(cli, &json!({"type": "profile-delete", "name": name}), true)?;
        }
    }
    Ok(())
}

/// A new connection to the Wayland display, for the daemon to hand to the booth; None where
/// there is none, as over ssh, which still lets a cached token be answered.
fn wayland_connection() -> Option<UnixStream> {
    let display = std::env::var_os("WAYLAND_DISPLAY")?;
    let path = if Path::new(&display).is_absolute() {
        PathBuf::from(display)
    } else {
        PathBuf::from(std::env::var_os("XDG_RUNTIME_DIR")?).join(display)
    };
    UnixStream::connect(path).ok()
}

/// The profile's key, from the vault the daemon named, made on first use.
fn profile_key(cli: &Cli, vault: &str, name: &str) -> Result<zeroize::Zeroizing<String>> {
    profile::check_name("vault", vault)?;
    let root = cli.pidvault_mount.join("stored").join(vault);
    if !root.is_dir() {
        bail!("the vault {vault:?} is not visible at {}: unlock it in this shell", root.display());
    }
    let dir = root.join("tokenbooth");
    let path = dir.join(format!("{name}.key"));
    match std::fs::read_to_string(&path) {
        Ok(key) => return Ok(zeroize::Zeroizing::new(key)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e).with_context(|| format!("cannot read {}", path.display())),
    }
    match DirBuilder::new().mode(0o700).create(&dir) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(e) => return Err(e).with_context(|| format!("cannot create {}", dir.display())),
    }
    let key = sealed::new_key()?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .with_context(|| format!("cannot create {}", path.display()))?;
    file.write_all(format!("{}\n", key.as_str()).as_bytes())?;
    Ok(key)
}

fn login(cli: &Cli, args: &LoginArgs, refresh: bool) -> Result<ExitCode> {
    let stream = UnixStream::connect(&cli.socket).with_context(|| format!("cannot reach the daemon at {}", cli.socket.display()))?;
    let vault = connect_pidvault(&cli.pidvault_socket)?;
    let display = wayland_connection();
    let fds: Vec<_> = [Some(vault.as_raw_fd()), display.as_ref().map(AsRawFd::as_raw_fd)].into_iter().flatten().collect();
    frame::send(&stream, &json!({"type": "login", "name": args.name, "refresh": refresh, "log": args.verbose}), &fds)?;
    drop((vault, display));
    let mut vault_name = None;
    loop {
        let (reply, _) = frame::receive(&stream)?.context("the daemon closed the connection without a result")?;
        match reply["type"].as_str() {
            Some("need-key") => {
                let vault = reply["vault"].as_str().unwrap_or_default().to_owned();
                let key = profile_key(cli, &vault, &args.name)?;
                vault_name = Some(vault);
                frame::send(&stream, &json!({"type": "key", "key": key.as_str()}), &[])?;
            }
            Some("log") => eprintln!("tokenbooth: {}", reply["text"].as_str().unwrap_or_default()),
            Some("error") => bail!("{}", reply["message"].as_str().unwrap_or("refused")),
            Some("result") => {
                return Ok(match (reply["outcome"].as_str(), reply["token"].as_str()) {
                    (Some("token"), Some(token)) => {
                        if args.to_vault {
                            let vault = vault_name.as_deref().context("the daemon never named the profile's vault")?;
                            let path = write_to_vault(cli, vault, &args.name, token)?;
                            eprintln!("tokenbooth: token written to {}", path.display());
                        } else if args.json {
                            println!("{}", json!({"token": token, "expires": reply["expires"]}));
                        } else {
                            println!("{token}");
                        }
                        ExitCode::SUCCESS
                    }
                    (Some("timed_out"), _) => {
                        eprintln!("tokenbooth: timed out");
                        ExitCode::from(EXIT_TIMEOUT)
                    }
                    _ => {
                        eprintln!("tokenbooth: closed without a token");
                        ExitCode::from(EXIT_CANCELLED)
                    }
                });
            }
            other => bail!("unexpected reply {other:?}"),
        }
    }
}

/// Writes the token to tokens/NAME in the vault, whole or not at all.
fn write_to_vault(cli: &Cli, vault: &str, name: &str, token: &str) -> Result<PathBuf> {
    profile::check_name("vault", vault)?;
    profile::check_name("profile", name)?;
    let dir = cli.pidvault_mount.join("stored").join(vault).join("tokens");
    match DirBuilder::new().mode(0o700).create(&dir) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(e) => return Err(e).with_context(|| format!("cannot create {}", dir.display())),
    }
    let path = dir.join(name);
    let partial = dir.join(format!(".{name}.new"));
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&partial)
        .with_context(|| format!("cannot write {}", partial.display()))?;
    file.write_all(format!("{token}\n").as_bytes())?;
    file.sync_all()?;
    std::fs::rename(&partial, &path).with_context(|| format!("cannot write {}", path.display()))?;
    Ok(path)
}

fn forget(cli: &Cli, name: &str) -> Result<()> {
    let reply = call(cli, &json!({"type": "forget", "name": name}), true)?;
    if reply["forgotten"] == true {
        eprintln!("tokenbooth: forgot the token and kept cookies of {name:?}");
    } else {
        eprintln!("tokenbooth: nothing kept for {name:?}");
    }
    Ok(())
}

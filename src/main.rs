//! tokenbooth: logins in a locked-down browser, for shells that pidvault has unlocked.

mod daemon;
mod frame;
mod pidvault;
mod procs;
mod profile;
mod systemd;

use std::io::Read;
use std::os::fd::AsRawFd;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use serde_json::{Value, json};

const SOCKET: &str = "/run/tokenbooth/sock";
const PIDVAULT_SOCKET: &str = "/run/pidvault.sock";
const STATE: &str = "/var/lib/tokenbooth";

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    /// The daemon's socket
    #[arg(long, env = "TOKENBOOTH_SOCKET", default_value = SOCKET, global = true)]
    socket: PathBuf,
    /// pidvault's control socket, which the daemon asks whether this shell is unlocked
    #[arg(long, env = "PIDVAULT_SOCKET", default_value = PIDVAULT_SOCKET, global = true)]
    pidvault_socket: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
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
        Command::Status => status(&cli),
        Command::Profile(command) => profile(&cli, command),
        Command::Daemon { listen, state, pidvault_user } => {
            run_daemon(listen.clone(), state.clone(), pidvault_user, &cli.pidvault_socket)
        }
    };
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("tokenbooth: {e:#}");
            ExitCode::from(2)
        }
    }
}

fn run_daemon(listen: Option<PathBuf>, state: PathBuf, pidvault_user: &str, pidvault_socket: &Path) -> Result<()> {
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
    daemon::run(listener, daemon::Config { state, pidvault })
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

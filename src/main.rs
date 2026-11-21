//! tokenbooth: logins in a locked-down browser, for shells that pidvault has unlocked.

mod daemon;
mod frame;
mod procs;
mod systemd;

use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::process::ExitCode;

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use serde_json::{Value, json};

const SOCKET: &str = "/run/tokenbooth/sock";
const STATE: &str = "/var/lib/tokenbooth";

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    /// The daemon's socket
    #[arg(long, env = "TOKENBOOTH_SOCKET", default_value = SOCKET, global = true)]
    socket: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Show who the daemon takes you to be
    Status,
    /// Run the daemon (started by systemd as the tokenbooth user)
    Daemon {
        /// Listen here instead of on the socket systemd passes
        #[arg(long, hide = true)]
        listen: Option<PathBuf>,
        /// Keep each caller's state under this directory
        #[arg(long, default_value = STATE)]
        state: PathBuf,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let outcome = match cli.command {
        Command::Status => status(&cli.socket),
        Command::Daemon { listen, state } => run_daemon(listen, state),
    };
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("tokenbooth: {e:#}");
            ExitCode::from(2)
        }
    }
}

fn run_daemon(listen: Option<PathBuf>, state: PathBuf) -> Result<()> {
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
    daemon::run(listener, daemon::Config { state })
}

fn call(socket: &PathBuf, request: &Value) -> Result<frame::Message> {
    let stream = UnixStream::connect(socket).with_context(|| format!("cannot reach the daemon at {}", socket.display()))?;
    frame::send(&stream, request, &[])?;
    let (reply, _) = frame::receive(&stream)?.context("the daemon closed the connection without a reply")?;
    if reply["type"] == "error" {
        bail!("{}", reply["message"].as_str().unwrap_or("refused"));
    }
    Ok(reply)
}

fn status(socket: &PathBuf) -> Result<()> {
    let reply = call(socket, &json!({"type": "status"}))?;
    println!("uid   {}", reply["uid"]);
    println!("pid   {} (this command)", reply["pid"]);
    println!("state {}", reply["state"].as_str().unwrap_or("?"));
    Ok(())
}

//! Profiles: what to open and what to capture, kept by the daemon per caller. The format is
//! the one `tokenbooth-dev` reads from `<home>/profiles/`, plus `vault`.

use std::fs::{DirBuilder, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Deserialize;

pub const PROVIDERS: &[&str] = &["google", "microsoft"];

#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    pub start_url: String,
    #[serde(default)]
    pub allowed_hosts: Option<Vec<String>>,
    pub cookie: String,
    #[serde(default)]
    pub cookie_domain: Option<String>,
    #[serde(default)]
    pub done_url: String,
    #[serde(default)]
    pub keep_cookies: Vec<String>,
    #[serde(default)]
    pub user_agent: String,
    #[serde(default = "default_timeout")]
    pub timeout: u32,
    #[serde(default)]
    pub via: Vec<String>,
    /// The pidvault vault that must be unlocked to use or change this profile.
    #[serde(default = "default_vault")]
    pub vault: String,
}

fn default_timeout() -> u32 {
    600
}

fn default_vault() -> String {
    "default".to_owned()
}

/// Names of profiles and vaults: letters, digits, `.`, `_` and `-`, not starting with `.`.
pub fn check_name(what: &str, name: &str) -> Result<()> {
    let ok = !name.is_empty()
        && name.len() <= 64
        && !name.starts_with('.')
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b));
    if !ok {
        bail!(
            "bad {what} name {name:?}: use letters, digits, '.', '_' and '-', not starting with '.'"
        );
    }
    Ok(())
}

pub fn host_of(url: &str) -> Option<&str> {
    let rest = url.strip_prefix("https://")?;
    let authority = rest.split(['/', '?', '#']).next()?;
    if authority.contains('@') {
        return None;
    }
    let host = authority
        .rsplit_once(':')
        .map_or(authority, |(host, _)| host);
    let ok = !host.is_empty()
        && host
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".-".contains(&b));
    ok.then_some(host)
}

impl Profile {
    pub fn parse(text: &str) -> Result<Profile> {
        let profile: Profile = toml::from_str(text).context("not a valid profile")?;
        if host_of(&profile.start_url).is_none() {
            bail!(
                "start_url must be an https URL with a plain host: {}",
                profile.start_url
            );
        }
        if profile.cookie.is_empty() {
            bail!("cookie must name the cookie that holds the token");
        }
        for pattern in &profile.keep_cookies {
            match pattern.split_once('@') {
                Some((name, domain)) if !name.is_empty() && !domain.is_empty() => {}
                _ => bail!("keep_cookies entry must be NAME@DOMAIN: {pattern:?}"),
            }
        }
        for provider in &profile.via {
            if !PROVIDERS.contains(&provider.as_str()) {
                bail!(
                    "unknown identity provider {provider:?} (known: {})",
                    PROVIDERS.join(", ")
                );
            }
        }
        for host in profile.allowed_hosts.iter().flatten() {
            if host.is_empty() || host.contains(char::is_whitespace) {
                bail!("bad host in allowed_hosts: {host:?}");
            }
        }
        check_name("vault", &profile.vault)?;
        Ok(profile)
    }
}

/// The profiles of one caller, in a directory of the daemon's.
pub struct Store {
    dir: PathBuf,
}

impl Store {
    pub fn open(state: &Path) -> Result<Store> {
        let dir = state.join("profiles");
        match DirBuilder::new().mode(0o700).create(&dir) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e).with_context(|| format!("cannot create {}", dir.display())),
        }
        Ok(Store { dir })
    }

    fn path(&self, name: &str) -> Result<PathBuf> {
        check_name("profile", name)?;
        Ok(self.dir.join(format!("{name}.toml")))
    }

    /// The profile's text as it was set, and the profile; None if there is none.
    pub fn load(&self, name: &str) -> Result<Option<(String, Profile)>> {
        let text = match std::fs::read_to_string(self.path(name)?) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e.into()),
        };
        let profile = Profile::parse(&text)?;
        Ok(Some((text, profile)))
    }

    pub fn save(&self, name: &str, text: &str) -> Result<()> {
        let path = self.path(name)?;
        let partial = self.dir.join(format!(".{name}.toml.new"));
        let mut file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&partial)?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
        std::fs::rename(&partial, &path)?;
        Ok(())
    }

    pub fn delete(&self, name: &str) -> Result<()> {
        std::fs::remove_file(self.path(name)?)?;
        Ok(())
    }

    pub fn names(&self) -> Result<Vec<String>> {
        let mut names = Vec::new();
        for entry in std::fs::read_dir(&self.dir)? {
            let file = entry?.file_name();
            let Some(name) = file.to_str().and_then(|f| f.strip_suffix(".toml")) else {
                continue;
            };
            if check_name("profile", name).is_ok() {
                names.push(name.to_owned());
            }
        }
        names.sort();
        Ok(names)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const GOPRO: &str = r#"
start_url     = "https://gopro.com/login"
cookie        = "gp_access_token"
cookie_domain = "gopro.com"
via           = ["google"]
keep_cookies  = ["*@google.com", "*@*.google.com"]
"#;

    #[test]
    fn the_example_profile_parses_with_defaults() {
        let profile = Profile::parse(GOPRO).unwrap();
        assert_eq!(profile.vault, "default");
        assert_eq!(profile.timeout, 600);
        assert_eq!(profile.via, ["google"]);
    }

    #[test]
    fn bad_profiles_are_refused() {
        for (text, complaint) in [
            ("start_url = \"http://x.example/\"\ncookie = \"t\"", "https"),
            (
                "start_url = \"https://u@x.example/\"\ncookie = \"t\"",
                "https",
            ),
            (
                "start_url = \"https://x.example/\"\ncookie = \"\"",
                "cookie",
            ),
            (
                "start_url = \"https://x.example/\"\ncookie = \"t\"\nkeep_cookies = [\"SID\"]",
                "NAME@DOMAIN",
            ),
            (
                "start_url = \"https://x.example/\"\ncookie = \"t\"\nvia = [\"evil\"]",
                "unknown identity provider",
            ),
            (
                "start_url = \"https://x.example/\"\ncookie = \"t\"\nvault = \"../x\"",
                "bad vault name",
            ),
            (
                "start_url = \"https://x.example/\"\ncookie = \"t\"\npersist_session = true",
                "not a valid profile",
            ),
        ] {
            let error = format!("{:#}", Profile::parse(text).unwrap_err());
            assert!(error.contains(complaint), "{text}: {error}");
        }
    }

    #[test]
    fn the_store_keeps_text_as_given_and_refuses_bad_names() {
        let state = tempfile::tempdir().unwrap();
        let store = Store::open(state.path()).unwrap();
        store.save("gopro", GOPRO).unwrap();
        assert_eq!(store.load("gopro").unwrap().unwrap().0, GOPRO);
        assert_eq!(store.names().unwrap(), ["gopro"]);
        assert!(store.load("../gopro").is_err());
        assert!(store.load(".hidden").is_err());
        store.delete("gopro").unwrap();
        assert!(store.load("gopro").unwrap().is_none());
    }
}

//! What the daemon keeps per profile: the token, its expiry and the keep-set, sealed with a key
//! that lives in the owner's vault. The daemon has the blob but not the key; an unlocked shell
//! has the key but not the blob. They meet only inside the daemon, for one request.

use std::fs::{DirBuilder, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use chacha20poly1305::aead::{Aead, Generate, KeyInit, Payload};
use chacha20poly1305::{XChaCha20Poly1305, XNonce};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use zeroize::Zeroizing;

use crate::profile::check_name;

const MAGIC: &[u8; 4] = b"TBS1";
const NONCE: usize = 24;
pub const KEY_BYTES: usize = 32;

/// A profile's key, as the client read it from the vault.
pub struct Key(Zeroizing<[u8; KEY_BYTES]>);

impl Key {
    pub fn from_base64(text: &str) -> Result<Key> {
        let bytes = Zeroizing::new(STANDARD.decode(text.trim()).context("the key is not base64")?);
        let array: [u8; KEY_BYTES] = bytes.as_slice().try_into().ok().context("the key is not 32 bytes")?;
        Ok(Key(Zeroizing::new(array)))
    }

    fn cipher(&self) -> XChaCha20Poly1305 {
        XChaCha20Poly1305::new(&(*self.0).into())
    }
}

/// A new key, base64, for the client to store in the vault.
pub fn new_key() -> Result<Zeroizing<String>> {
    let mut bytes = Zeroizing::new([0u8; KEY_BYTES]);
    getrandom::fill(&mut *bytes).map_err(|e| anyhow::anyhow!("no randomness: {e}"))?;
    Ok(Zeroizing::new(STANDARD.encode(*bytes)))
}

#[derive(Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct State {
    pub token: Option<String>,
    pub expires: Option<f64>,
    /// Keep entries as the booth returned them; opaque but for name, domain, path and expires.
    pub keep: Vec<Value>,
}

impl Drop for State {
    fn drop(&mut self) {
        use zeroize::Zeroize;
        if let Some(token) = &mut self.token {
            token.zeroize();
        }
    }
}

/// Binds a blob to the uid and profile it was sealed for, so one cannot stand in for another.
fn binding(uid: u32, profile: &str) -> Vec<u8> {
    format!("tokenbooth state v1\0uid={uid}\0profile={profile}").into_bytes()
}

pub fn seal(key: &Key, uid: u32, profile: &str, state: &State) -> Result<Vec<u8>> {
    let plain = Zeroizing::new(serde_json::to_vec(state)?);
    let nonce = XNonce::generate();
    let aad = binding(uid, profile);
    let sealed = key
        .cipher()
        .encrypt(&nonce, Payload { msg: &plain, aad: &aad })
        .map_err(|_| anyhow::anyhow!("cannot seal"))?;
    Ok([MAGIC.as_slice(), nonce.as_slice(), &sealed].concat())
}

pub fn open(key: &Key, uid: u32, profile: &str, blob: &[u8]) -> Result<State> {
    if blob.len() < MAGIC.len() + NONCE || &blob[..MAGIC.len()] != MAGIC {
        bail!("not a sealed state");
    }
    let (nonce, sealed) = blob[MAGIC.len()..].split_at(NONCE);
    let nonce = XNonce::try_from(nonce).map_err(|_| anyhow::anyhow!("bad nonce"))?;
    let aad = binding(uid, profile);
    let plain = Zeroizing::new(
        key.cipher()
            .decrypt(&nonce, Payload { msg: sealed, aad: &aad })
            .map_err(|_| anyhow::anyhow!("the key does not open it"))?,
    );
    Ok(serde_json::from_slice(&plain)?)
}

/// The sealed states of one caller, in a directory of the daemon's.
pub struct Vaulted {
    dir: PathBuf,
    uid: u32,
}

/// What loading found.
pub enum Loaded {
    Fresh,
    Opened(State),
    /// There is a blob, but this key does not open it: the key was replaced or the vault is
    /// another. Its contents are lost; the next save replaces it.
    Unreadable,
}

impl Vaulted {
    pub fn open(state: &Path, uid: u32) -> Result<Vaulted> {
        let dir = state.join("sealed");
        match DirBuilder::new().mode(0o700).create(&dir) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e).with_context(|| format!("cannot create {}", dir.display())),
        }
        Ok(Vaulted { dir, uid })
    }

    fn path(&self, name: &str) -> Result<PathBuf> {
        check_name("profile", name)?;
        Ok(self.dir.join(name))
    }

    pub fn load(&self, name: &str, key: &Key) -> Result<Loaded> {
        let blob = match std::fs::read(self.path(name)?) {
            Ok(blob) => blob,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Loaded::Fresh),
            Err(e) => return Err(e.into()),
        };
        Ok(match open(key, self.uid, name, &blob) {
            Ok(state) => Loaded::Opened(state),
            Err(_) => Loaded::Unreadable,
        })
    }

    pub fn save(&self, name: &str, key: &Key, state: &State) -> Result<()> {
        let blob = seal(key, self.uid, name, state)?;
        let partial = self.dir.join(format!(".{name}.new"));
        let mut file = OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&partial)?;
        file.write_all(&blob)?;
        file.sync_all()?;
        std::fs::rename(&partial, self.path(name)?)?;
        Ok(())
    }

    pub fn forget(&self, name: &str) -> Result<bool> {
        match std::fs::remove_file(self.path(name)?) {
            Ok(()) => Ok(true),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(e.into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn state() -> State {
        State { token: Some("t0k3n".into()), expires: Some(1.5e9), keep: vec![json!({"name": "SID"})] }
    }

    #[test]
    fn a_sealed_state_opens_only_with_its_key_uid_and_profile() {
        let key = Key::from_base64(&new_key().unwrap()).unwrap();
        let blob = seal(&key, 1000, "gopro", &state()).unwrap();
        assert!(!String::from_utf8_lossy(&blob).contains("t0k3n"));
        assert_eq!(open(&key, 1000, "gopro", &blob).unwrap(), state());
        assert!(open(&key, 1001, "gopro", &blob).is_err());
        assert!(open(&key, 1000, "github", &blob).is_err());
        let other = Key::from_base64(&new_key().unwrap()).unwrap();
        assert!(open(&other, 1000, "gopro", &blob).is_err());
        let mut tampered = blob.clone();
        *tampered.last_mut().unwrap() ^= 1;
        assert!(open(&key, 1000, "gopro", &tampered).is_err());
    }

    #[test]
    fn keys_must_be_32_bytes_of_base64() {
        assert!(Key::from_base64("not base64!").is_err());
        assert!(Key::from_base64(&STANDARD.encode([0u8; 16])).is_err());
        assert!(Key::from_base64(&format!("{}\n", STANDARD.encode([7u8; 32]))).is_ok());
    }

    #[test]
    fn the_store_saves_loads_and_forgets() {
        let dir = tempfile::tempdir().unwrap();
        let store = Vaulted::open(dir.path(), 1000).unwrap();
        let key = Key::from_base64(&new_key().unwrap()).unwrap();
        assert!(matches!(store.load("gopro", &key).unwrap(), Loaded::Fresh));
        store.save("gopro", &key, &state()).unwrap();
        assert!(matches!(store.load("gopro", &key).unwrap(), Loaded::Opened(s) if s == state()));
        let other = Key::from_base64(&new_key().unwrap()).unwrap();
        assert!(matches!(store.load("gopro", &other).unwrap(), Loaded::Unreadable));
        assert!(store.forget("gopro").unwrap());
        assert!(!store.forget("gopro").unwrap());
        assert!(store.load("../x", &key).is_err());
    }
}

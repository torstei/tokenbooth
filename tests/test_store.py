import datetime
import json
import os
import stat
import time

import pytest

from tokenbooth import store


def test_host_glob_star_is_one_label():
    assert store.host_matches("accounts.google.no", "accounts.google.*")
    assert not store.host_matches("accounts.google.evil.com", "accounts.google.*")
    assert store.host_matches("a.b.gopro.com", "*.gopro.com")
    assert not store.host_matches("gopro.com", "*.gopro.com")


def test_keep_matches_name_glob_and_domain_without_leading_dot():
    patterns = ["SID@google.com", "__Secure-*@*.google.com"]
    assert store.keep_matches(patterns, "SID", ".google.com")
    assert store.keep_matches(patterns, "__Secure-1PSID", "accounts.google.com")
    assert not store.keep_matches(patterns, "HSID", ".google.com")
    assert not store.keep_matches(patterns, "SID", ".google.evil.com")
    assert not store.keep_matches([], "SID", ".google.com")


@pytest.mark.parametrize("pattern", ["SID", "@google.com", "SID@"])
def test_keep_pattern_needs_name_and_domain(pattern):
    with pytest.raises(store.UsageError):
        store.keep_pattern(pattern)


def write_profile(home, text):
    (home / "profiles").mkdir(parents=True)
    (home / "profiles" / "p.toml").write_text(text)


def test_profile_rejects_persist_session(tmp_path):
    write_profile(tmp_path, 'start_url = "https://x.example/"\ncookie = "t"\npersist_session = true\n')
    with pytest.raises(store.UsageError, match="keep_cookies"):
        store.Profile.load(tmp_path, "p", {})


def test_profile_rejects_bad_keep_pattern(tmp_path):
    write_profile(tmp_path, 'start_url = "https://x.example/"\ncookie = "t"\nkeep_cookies = ["SID"]\n')
    with pytest.raises(store.UsageError, match="NAME@DOMAIN"):
        store.Profile.load(tmp_path, "p", {})


def test_keep_on_the_command_line_replaces_the_profiles(tmp_path):
    write_profile(tmp_path, 'start_url = "https://x.example/"\ncookie = "t"\nkeep_cookies = ["*@google.com"]\n')
    profile = store.Profile.load(tmp_path, "p", {"keep_cookies": ["SID@google.com"]})
    assert profile.keep_cookies == ["SID@google.com"]


def test_keep_set_is_private_and_drops_expired(tmp_path):
    now = time.time()
    entries = [
        {"name": "live", "domain": ".google.com", "path": "/", "expires": now + 3600, "raw": ""},
        {"name": "session", "domain": ".google.com", "path": "/", "expires": None, "raw": ""},
        {"name": "dead", "domain": ".google.com", "path": "/", "expires": now - 1, "raw": ""},
    ]
    store.save_keep(tmp_path, "p", entries)
    path = store.keep_path(tmp_path, "p")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert [e["name"] for e in store.load_keep(tmp_path, "p")] == ["live", "session"]
    assert len(json.loads(path.read_text())) == 3


def test_forget_removes_token_and_keep_set(tmp_path):
    store.save_token(tmp_path, "p", "tok", time.time() + 3600)
    store.save_keep(tmp_path, "p", [])
    removed = store.forget(tmp_path, "p")
    assert set(removed) == {store.token_path(tmp_path, "p"), store.keep_path(tmp_path, "p")}


def test_firefox_version_follows_the_release_cycle_and_never_runs_ahead():
    known, since = store.FIREFOX_KNOWN
    day = datetime.timedelta(days=1)
    assert f"Firefox/{known}.0" in store.firefox_ua(since)
    assert f"Firefox/{known}.0" in store.firefox_ua(since + 27 * day)
    assert f"rv:{known + 1}.0" in store.firefox_ua(since + 28 * day)
    assert f"Firefox/{known}.0" in store.firefox_ua(since - 100 * day)


def test_via_google_claims_a_current_firefox_on_qt_only(tmp_path):
    write_profile(tmp_path, 'start_url = "https://x.example/"\ncookie = "t"\nvia = ["google"]\n')
    assert store.Profile.load(tmp_path, "p", {}, engine="qt").user_agent == store.firefox_ua()
    assert store.Profile.load(tmp_path, "p", {}, engine="chrome").user_agent == ""


@pytest.mark.parametrize("argv", [
    ["--home", "/h", "-v", "login", "gopro"],
    ["login", "gopro", "--home", "/h", "-v"],
    ["--home", "/h", "login", "-v", "gopro"],
])
def test_common_options_before_or_after_the_subcommand(argv):
    from tokenbooth.cli import parse_args
    args = parse_args(argv)
    assert (str(args.home), args.verbose, args.name) == ("/h", True, "gopro")


def test_common_options_default_when_absent():
    from tokenbooth.cli import parse_args
    args = parse_args(["keep-set", "gopro"])
    assert (args.home, args.verbose) == (None, False)


def test_via_microsoft_allows_its_hosts_and_keeps_the_engines_user_agent(tmp_path):
    write_profile(tmp_path, 'start_url = "https://app.example.com/"\ncookie = "t"\nvia = ["microsoft"]\n')
    profile = store.Profile.load(tmp_path, "p", {})
    assert profile.user_agent == ""
    assert {"login.microsoftonline.com", "login.live.com"} <= set(profile.allowed_hosts)
    assert "app.example.com" in profile.allowed_hosts


def test_unknown_provider_names_the_known_ones(tmp_path):
    write_profile(tmp_path, 'start_url = "https://app.example.com/"\ncookie = "t"\nvia = ["okta"]\n')
    with pytest.raises(store.UsageError, match="google, microsoft"):
        store.Profile.load(tmp_path, "p", {})

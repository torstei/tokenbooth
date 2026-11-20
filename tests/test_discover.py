from tokenbooth import discover, store

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


def cookie(r, name, domain, value="v" * 8, path="/", http_only=False, secure=True, session=False):
    r.cookie_set(name, domain, path, value, http_only=http_only, secure=secure, session=session)


def test_site_of_drops_www_and_the_first_of_three_labels():
    assert discover.site_of("github.com") == "github.com"
    assert discover.site_of("www.gopro.com") == "gopro.com"
    assert discover.site_of("my.example.com") == "example.com"


def test_github_through_google_picks_the_session_not_what_existed_before():
    r = discover.Recorder()
    r.navigated("github.com", "/login")
    cookie(r, "_gh_sess", "github.com", "s" * 200, http_only=True)
    cookie(r, "logged_in", ".github.com", "no", http_only=True)
    cookie(r, "_device_id", "github.com", "d" * 32, http_only=True)
    r.navigated("github.com", "/sessions/social/google/initiate")
    r.navigated("accounts.google.com", "/o/oauth2/v2/auth")
    cookie(r, "SID", ".google.com", "g" * 70)
    r.navigated("github.com", "/sessions/social/google/callback")
    r.navigated("github.com", "/sessions/two-factor/mobile")
    cookie(r, "saved_user_sessions", "github.com", "1234567:" + "u" * 48, http_only=True)
    cookie(r, "user_session", "github.com", "u" * 48, http_only=True)
    cookie(r, "__Host-user_session_same_site", "github.com", "u" * 48, http_only=True)
    cookie(r, "logged_in", ".github.com", "yes", http_only=True)
    cookie(r, "dotcom_user", ".github.com", "someone", http_only=True)
    cookie(r, "_gh_sess", "github.com", "t" * 200, http_only=True)

    names = [s.name for s in r.candidates("github.com")]
    assert names[0] == "user_session"
    assert set(names) == {"user_session", "__Host-user_session_same_site", "saved_user_sessions", "dotcom_user"}
    assert [s.name for s in discover.tied(r.candidates("github.com"))] == ["user_session", "__Host-user_session_same_site"]


def test_entra_app_ignores_a_session_id_that_was_merely_replaced():
    r = discover.Recorder()
    r.navigated("my.example.com", "/app")
    cookie(r, "JSESSIONID", ".my.example.com", "j" * 32, path="/app", http_only=True)
    r.navigated("my.example.com", "/app/security/oauth2/authorization/login")
    r.navigated("login.microsoftonline.com", "/common/oauth2/v2.0/authorize")
    r.navigated("auth.example.com", "/redirect/auth")
    cookie(r, "JSESSIONID", ".my.example.com", "k" * 32, path="/app", http_only=True)
    cookie(r, "X-ACCESS-TOKEN", ".example.com", JWT)
    cookie(r, "X-REFRESH-TOKEN", ".example.com", "r" * 40)

    best, *rest = r.candidates(discover.site_of("my.example.com"))
    assert best.name == "X-ACCESS-TOKEN" and best.jwt
    assert [s.name for s in rest] == ["X-REFRESH-TOKEN"]


def test_gopro_through_google_picks_the_access_token():
    r = discover.Recorder()
    r.navigated("gopro.com", "/login")
    cookie(r, "_dd_s", "gopro.com", "x" * 60)
    cookie(r, "OptanonConsent", ".gopro.com", "o" * 300)
    r.navigated("api.gopro.com", "/auth/google_oauth2")
    r.navigated("accounts.google.com", "/o/oauth2/auth")
    r.navigated("gopro.com", "/login/external-providers")
    cookie(r, "_dd_s", "gopro.com", "y" * 60)
    cookie(r, "gp_access_token", ".gopro.com", "eyJhbGciOiJSU0EtT0FFUCJ9.a.b.c.d")
    cookie(r, "gp_user_id", ".gopro.com", "1234567")
    cookie(r, "gp_logged_in", ".gopro.com", "true")

    assert r.candidates("gopro.com")[0].name == "gp_access_token"


def test_a_cookie_removed_again_is_no_candidate():
    r = discover.Recorder()
    r.navigated("app.example", "/")
    r.navigated("app.example", "/next")
    cookie(r, "flash", "app.example")
    r.cookie_removed("flash", "app.example", "/")
    assert r.candidates("app.example") == []


def test_without_leaving_the_start_page_nothing_is_a_candidate():
    r = discover.Recorder()
    r.navigated("app.example", "/login")
    cookie(r, "session", "app.example", "s" * 40, http_only=True)
    assert r.candidates("app.example") == []


def test_suggested_profile_has_only_what_the_defaults_and_providers_do_not(tmp_path):
    profile = store.Profile.load(tmp_path, "p", {"start_url": "https://my.example.com/app",
                                                 "keep_cookies": ["*@login.microsoftonline.com"]},
                                 extra_hosts=["*.example.com"], via=["microsoft"], require_cookie=False)
    best = discover.Seen("X-ACCESS-TOKEN", ".example.com", "/", order=0, before=False)
    text = discover.suggest_profile(profile, best)
    assert 'cookie        = "X-ACCESS-TOKEN"' in text
    assert 'cookie_domain = "example.com"' in text
    assert 'allowed_hosts = ["my.example.com", "*.my.example.com", "*.example.com"]' in text
    assert 'via           = ["microsoft"]' in text
    assert 'keep_cookies  = ["*@login.microsoftonline.com"]' in text

    plain = store.Profile.load(tmp_path, "q", {"start_url": "https://gopro.com/login"}, require_cookie=False)
    assert "allowed_hosts" not in discover.suggest_profile(plain, best)


def test_suggested_profile_loads_back(tmp_path):
    profile = store.Profile.load(tmp_path, "p", {"start_url": "https://gopro.com/login"},
                                 via=["google"], require_cookie=False)
    best = discover.Seen("gp_access_token", ".gopro.com", "/", order=0, before=False)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "gopro.toml").write_text(discover.suggest_profile(profile, best))
    loaded = store.Profile.load(tmp_path, "gopro", {})
    assert (loaded.cookie, loaded.cookie_domain, loaded.via) == ("gp_access_token", "gopro.com", ["google"])

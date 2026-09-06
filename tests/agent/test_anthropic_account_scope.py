"""Scoped Claude account resolution (own-fork issue #40).

Hermes resolved Claude Code credentials from a hard-coded
``~/.claude/.credentials.json`` regardless of ``CLAUDE_CONFIG_DIR``, so a
worker assigned an explicit account still called whichever account the global
file happened to hold — the exhausted one, in the incident that prompted this.

Everything here is synthetic: two temporary account directories, a fake
provider that reports *which* account it was called as, and a fake refresh
endpoint. No real credential file, keychain, network call or environment dump
is touched, and no test reads ``~/.claude``.
"""

import json
import os
import threading
from pathlib import Path

import pytest

import agent.anthropic_credentials as AC


# ---------------------------------------------------------------- fixtures


def _write_account(directory: Path, *, access, refresh="rt-x", expires_at_ms=None):
    """Create a synthetic Claude account directory and return its cred file."""
    import time

    if expires_at_ms is None:
        expires_at_ms = int(time.time() * 1000) + 3_600_000
    directory.mkdir(parents=True, exist_ok=True)
    cred = directory / ".credentials.json"
    cred.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": access,
                    "refreshToken": refresh,
                    "expiresAt": expires_at_ms,
                    "scopes": ["user:inference"],
                }
            }
        ),
        encoding="utf-8",
    )
    return cred


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    """Two synthetic accounts plus a synthetic 'global' ~/.claude.

    ``Path.home()`` is redirected at the module under test so the unscoped
    fallback resolves inside tmp_path and never near the real home directory.
    """
    home = tmp_path / "home"
    _write_account(home / ".claude", access="tok-global", refresh="rt-global")
    _write_account(tmp_path / "acct-a", access="tok-account-a", refresh="rt-a")
    _write_account(tmp_path / "acct-b", access="tok-account-b", refresh="rt-b")

    monkeypatch.setattr(AC.Path, "home", classmethod(lambda cls: home))
    # The keychain reader shells out to macOS `security`; never let a test host
    # reach it. Scoped reads must not consult it at all (asserted below).
    monkeypatch.setattr(
        AC, "_read_claude_code_credentials_from_keychain", lambda: None
    )
    # No ambient env credential may leak into these resolutions.
    for var in (
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return {
        "home": home,
        "global": home / ".claude",
        "a": tmp_path / "acct-a",
        "b": tmp_path / "acct-b",
        "root": tmp_path,
    }


def fake_provider(token):
    """Stand-in for the Anthropic API: reports which account a token belongs to.

    Deliberately not a mock of Hermes internals — it is the only thing that can
    answer "which account did the request actually go out as?", which is the
    claim under test.
    """
    known = {
        "tok-global": "global",
        "tok-account-a": "account-a",
        "tok-account-b": "account-b",
    }
    return known.get(token, "unknown")


# ------------------------------------------------------- path resolution


class TestScopedPathResolution:
    def test_unset_resolves_the_global_file(self, accounts):
        assert AC.claude_config_dir() is None
        assert AC.claude_code_credentials_path() == accounts["global"] / ".credentials.json"

    def test_explicit_directory_is_honoured(self, accounts, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["a"]))
        assert AC.claude_code_credentials_path() == accounts["a"] / ".credentials.json"

    def test_blank_value_is_treated_as_unset(self, accounts, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
        assert AC.claude_config_dir() is None
        assert AC.claude_code_credentials_path() == accounts["global"] / ".credentials.json"

    def test_tilde_is_expanded(self, accounts, monkeypatch):
        monkeypatch.setenv("HOME", str(accounts["root"]))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/acct-b")
        assert AC.claude_code_credentials_path() == accounts["b"] / ".credentials.json"


# ------------------------------------------------- which account is used


class TestSelectedAccountIsTheOneUsed:
    def test_two_accounts_resolve_to_their_own_token(self, accounts, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["a"]))
        assert fake_provider(AC.resolve_anthropic_token()) == "account-a"

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["b"]))
        assert fake_provider(AC.resolve_anthropic_token()) == "account-b"

    def test_unset_still_resolves_the_global_account(self, accounts):
        assert fake_provider(AC.resolve_anthropic_token()) == "global"

    def test_scoped_read_ignores_the_keychain(self, accounts, monkeypatch):
        """The keychain entry is global and account-blind, so a scoped read
        must not consult it — adopting it is the substitution under test."""
        called = []

        def _kc():
            called.append(True)
            return {
                "accessToken": "tok-global",
                "refreshToken": "rt-global",
                "expiresAt": 4_000_000_000_000,
            }

        monkeypatch.setattr(AC, "_read_claude_code_credentials_from_keychain", _kc)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["a"]))
        assert fake_provider(AC.resolve_anthropic_token()) == "account-a"
        assert called == []

        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        AC.resolve_anthropic_token()
        assert called, "unscoped resolution must still consult the keychain"


class TestNoGlobalSubstitution:
    def test_empty_scoped_account_does_not_fall_back_to_the_pool(
        self, accounts, monkeypatch
    ):
        """The incident: an explicitly selected account with nothing usable
        was answered with an unrelated global credential."""
        empty = accounts["root"] / "acct-empty"
        empty.mkdir()
        monkeypatch.setattr(
            AC, "_resolve_anthropic_pool_token", lambda: "tok-global"
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
        assert AC.resolve_anthropic_token() is None

    def test_pool_fallback_is_preserved_when_unset(self, accounts, monkeypatch):
        (accounts["global"] / ".credentials.json").unlink()
        monkeypatch.setattr(
            AC, "_resolve_anthropic_pool_token", lambda: "tok-global"
        )
        assert fake_provider(AC.resolve_anthropic_token()) == "global"

    def test_expired_scoped_account_that_cannot_refresh_returns_none(
        self, accounts, monkeypatch
    ):
        _write_account(
            accounts["a"], access="tok-account-a", refresh="", expires_at_ms=1
        )
        monkeypatch.setattr(
            AC, "_resolve_anthropic_pool_token", lambda: "tok-global"
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["a"]))
        assert AC.resolve_anthropic_token() is None


# ----------------------------------------------- existing env semantics


class TestExistingEnvSemanticsPreserved:
    def test_explicit_api_key_still_wins(self, accounts, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["a"]))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-explicit")
        assert AC.resolve_anthropic_token() == "sk-ant-api-explicit"

    def test_static_env_oauth_token_defers_to_the_scoped_refreshable_account(
        self, accounts, monkeypatch
    ):
        """Unchanged preference rule, now bounded to the selected account:
        the refreshable credential it prefers is account A's, never a global."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["a"]))
        monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-ant-oat-static")
        assert fake_provider(AC.resolve_anthropic_token()) == "account-a"

    def test_env_oauth_token_survives_when_the_scoped_account_is_empty(
        self, accounts, monkeypatch
    ):
        empty = accounts["root"] / "acct-empty2"
        empty.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-static")
        assert AC.resolve_anthropic_token() == "sk-ant-oat-static"


# ------------------------------------------------------ borrower symlink


class TestBorrowerSymlinkResolvesToOwner:
    def test_path_resolves_to_the_canonical_owner(self, accounts, monkeypatch):
        borrower = accounts["root"] / "borrower"
        borrower.mkdir()
        owner = accounts["a"] / ".credentials.json"
        (borrower / ".credentials.json").symlink_to(owner)

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(borrower))
        assert AC.claude_code_credentials_path() == owner
        assert fake_provider(AC.resolve_anthropic_token()) == "account-a"

    def test_refresh_writes_through_to_the_owner_and_keeps_the_link(
        self, accounts, monkeypatch
    ):
        """An ``os.replace()`` onto the symlink would replace the link with a
        regular file, silently forking the borrower from its owner."""
        _write_account(
            accounts["a"], access="tok-old", refresh="rt-a", expires_at_ms=1
        )
        borrower = accounts["root"] / "borrower"
        borrower.mkdir()
        owner = accounts["a"] / ".credentials.json"
        link = borrower / ".credentials.json"
        link.symlink_to(owner)

        monkeypatch.setattr(
            AC,
            "refresh_anthropic_oauth_pure",
            lambda rt, use_json=False: {
                "access_token": "tok-account-a",
                "refresh_token": "rt-a2",
                "expires_at_ms": 4_000_000_000_000,
            },
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(borrower))

        assert fake_provider(AC.resolve_anthropic_token()) == "account-a"
        assert link.is_symlink(), "borrower link was replaced by a regular file"
        owner_data = json.loads(owner.read_text(encoding="utf-8"))
        assert owner_data["claudeAiOauth"]["accessToken"] == "tok-account-a"
        assert owner_data["claudeAiOauth"]["refreshToken"] == "rt-a2"
        # Scopes on the pre-existing owner file must survive the commit.
        assert owner_data["claudeAiOauth"]["scopes"] == ["user:inference"]
        # No divergent copy left beside the link.
        assert sorted(p.name for p in borrower.iterdir()) == [".credentials.json"]


class TestConcurrentRefreshIsSingleWriter:
    def test_two_borrowers_of_one_owner_spend_one_refresh_token(
        self, accounts, monkeypatch
    ):
        """Two borrower directories are two paths for ONE account. Locking the
        borrower path would give each its own lock and let both POST the same
        single-use refresh token."""
        _write_account(
            accounts["a"], access="tok-old", refresh="rt-a", expires_at_ms=1
        )
        owner = accounts["a"] / ".credentials.json"
        borrowers = []
        for name in ("b1", "b2"):
            d = accounts["root"] / name
            d.mkdir()
            (d / ".credentials.json").symlink_to(owner)
            borrowers.append(d)

        posts = []
        post_lock = threading.Lock()

        def _fake_refresh(refresh_token, use_json=False):
            with post_lock:
                posts.append(refresh_token)
            return {
                "access_token": "tok-account-a",
                "refresh_token": "rt-a2",
                "expires_at_ms": 4_000_000_000_000,
            }

        monkeypatch.setattr(AC, "refresh_anthropic_oauth_pure", _fake_refresh)

        # CLAUDE_CONFIG_DIR is process-wide, so give each thread its own value
        # through the module's own env accessor rather than mutating os.environ.
        local = threading.local()
        real_getenv = AC._getenv

        def _scoped_getenv(name, default=""):
            if name == "CLAUDE_CONFIG_DIR":
                return getattr(local, "dir", "")
            return real_getenv(name, default)

        monkeypatch.setattr(AC, "_getenv", _scoped_getenv)

        results = {}
        start = threading.Barrier(len(borrowers))

        def _run(idx, directory):
            local.dir = str(directory)
            start.wait(timeout=10)
            results[idx] = AC.resolve_anthropic_token()

        threads = [
            threading.Thread(target=_run, args=(i, d))
            for i, d in enumerate(borrowers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()

        assert len(posts) == 1, f"single-use refresh token POSTed {len(posts)} times"
        assert set(results.values()) == {"tok-account-a"}
        assert all(fake_provider(v) == "account-a" for v in results.values())


# ------------------------------------------------- failure and no-leak


class TestFailureModes:
    def test_missing_directory_returns_none_without_raising(
        self, accounts, monkeypatch
    ):
        monkeypatch.setenv(
            "CLAUDE_CONFIG_DIR", str(accounts["root"] / "does-not-exist")
        )
        assert AC.read_claude_code_credentials() is None
        assert AC.resolve_anthropic_token() is None

    def test_directory_that_is_a_file_returns_none(self, accounts, monkeypatch):
        bogus = accounts["root"] / "not-a-dir"
        bogus.write_text("x", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(bogus))
        assert AC.resolve_anthropic_token() is None

    def test_malformed_credentials_return_none(self, accounts, monkeypatch):
        (accounts["a"] / ".credentials.json").write_text(
            "{not json", encoding="utf-8"
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["a"]))
        assert AC.resolve_anthropic_token() is None

    def test_credentials_missing_the_oauth_block_return_none(
        self, accounts, monkeypatch
    ):
        (accounts["a"] / ".credentials.json").write_text(
            json.dumps({"somethingElse": {}}), encoding="utf-8"
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["a"]))
        assert AC.resolve_anthropic_token() is None

    def test_dangling_symlink_returns_none_and_is_not_materialised(
        self, accounts, monkeypatch
    ):
        borrower = accounts["root"] / "dangling"
        borrower.mkdir()
        link = borrower / ".credentials.json"
        link.symlink_to(accounts["root"] / "gone" / ".credentials.json")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(borrower))
        assert AC.resolve_anthropic_token() is None
        assert link.is_symlink()


class TestNoSecretLeak:
    def test_failure_logs_name_the_path_but_never_the_token(
        self, accounts, monkeypatch, caplog
    ):
        """A commit failure must be diagnosable without printing credentials."""
        _write_account(
            accounts["a"], access="tok-old", refresh="rt-secret-a", expires_at_ms=1
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts["a"]))
        monkeypatch.setattr(
            AC,
            "refresh_anthropic_oauth_pure",
            lambda rt, use_json=False: {
                "access_token": "tok-secret-new",
                "refresh_token": "rt-secret-new",
                "expires_at_ms": 4_000_000_000_000,
            },
        )

        def _boom(*a, **kw):
            raise AC.CredentialPersistError(
                accounts["a"] / ".credentials.json", OSError("disk full")
            )

        monkeypatch.setattr(AC, "_write_claude_code_credentials", _boom)

        with caplog.at_level("DEBUG", logger=AC.logger.name):
            assert AC.resolve_anthropic_token() is None

        text = caplog.text
        assert str(accounts["a"]) in text
        for secret in (
            "tok-old",
            "rt-secret-a",
            "tok-secret-new",
            "rt-secret-new",
        ):
            assert secret not in text, f"credential value {secret!r} was logged"

    def test_persist_error_message_carries_the_path_not_the_secret(self, tmp_path):
        err = str(AC.CredentialPersistError(tmp_path / "c.json", OSError("nope")))
        assert str(tmp_path) in err
        assert "nope" in err

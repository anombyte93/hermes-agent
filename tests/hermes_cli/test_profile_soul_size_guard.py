"""Profile creation refuses a SOUL.md over the 20,000-character boot limit.

A clone copies the source SOUL (``copy2`` follows a symlinked SOUL), so an oversized
legacy render silently became a new profile's boot prompt. Every path that creates a
profile from someone else's SOUL must refuse it before the profile dir exists.
"""

from pathlib import Path

import pytest

from hermes_cli.profiles import create_profile, get_profile_dir

# The approved boot limit; exactly this many characters is allowed.
SOUL_CHAR_LIMIT = 20_000


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def _make_source(name: str, soul_chars: int | None) -> Path:
    src = create_profile(name, no_alias=True)
    soul = src / "SOUL.md"
    soul.unlink(missing_ok=True)
    if soul_chars is not None:
        soul.write_text("s" * soul_chars, encoding="utf-8")
    return src


def _assert_refused(excinfo, size: int) -> None:
    msg = str(excinfo.value)
    assert "SOUL.md" in msg
    assert f"{size:,}" in msg
    assert f"{SOUL_CHAR_LIMIT:,}" in msg


def test_clone_refuses_soul_over_limit_and_leaves_no_profile_dir(profile_env):
    over = SOUL_CHAR_LIMIT + 1
    src = _make_source("big", over)
    with pytest.raises(ValueError) as excinfo:
        create_profile("copy", clone_from="big", no_alias=True)
    _assert_refused(excinfo, over)
    assert str(src / "SOUL.md") in str(excinfo.value)
    assert not get_profile_dir("copy").exists()


def test_clone_refuses_symlinked_soul_whose_target_is_over_limit(profile_env, tmp_path):
    over = SOUL_CHAR_LIMIT * 15
    src = _make_source("linked", None)
    target = tmp_path / "rendered" / "SOUL.md"
    target.parent.mkdir()
    target.write_text("r" * over, encoding="utf-8")
    try:
        (src / "SOUL.md").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError) as excinfo:
        create_profile("copy", clone_from="linked", no_alias=True)
    _assert_refused(excinfo, over)
    assert not get_profile_dir("copy").exists()


def test_clone_all_refuses_symlinked_oversize_soul(profile_env, tmp_path):
    over = SOUL_CHAR_LIMIT * 15
    src = _make_source("linked", None)
    target = tmp_path / "rendered" / "SOUL.md"
    target.parent.mkdir()
    target.write_text("r" * over, encoding="utf-8")
    try:
        (src / "SOUL.md").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError) as excinfo:
        create_profile("copy", clone_from="linked", clone_all=True, no_alias=True)
    _assert_refused(excinfo, over)
    assert not get_profile_dir("copy").exists()


def test_clone_allows_soul_exactly_at_limit(profile_env):
    _make_source("edge", SOUL_CHAR_LIMIT)
    for name, kwargs in (("copy", {}), ("copyall", {"clone_all": True})):
        dst = create_profile(name, clone_from="edge", no_alias=True, **kwargs)
        assert len((dst / "SOUL.md").read_text(encoding="utf-8")) == SOUL_CHAR_LIMIT


def test_clone_without_soul_still_seeds_default(profile_env):
    from hermes_cli.default_soul import DEFAULT_SOUL_MD

    _make_source("bare", None)
    dst = create_profile("copy", clone_from="bare", no_alias=True)
    assert (dst / "SOUL.md").read_text(encoding="utf-8") == DEFAULT_SOUL_MD


def test_distribution_install_refuses_oversize_soul(profile_env, tmp_path):
    from hermes_cli.profile_distribution import (
        DistributionError,
        DistributionManifest,
        install_distribution,
        write_manifest,
    )

    over = SOUL_CHAR_LIMIT + 1
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "SOUL.md").write_text("d" * over, encoding="utf-8")
    (staged / "config.yaml").write_text("model:\n  model: gpt-4\n")
    write_manifest(staged, DistributionManifest(name="dist", version="0.1.0"))
    with pytest.raises(DistributionError) as excinfo:
        install_distribution(str(staged), name="dist")
    _assert_refused(excinfo, over)
    assert not get_profile_dir("dist").exists()


def _make_profile_archive(tmp_path: Path, root: str, soul_chars: int) -> Path:
    import tarfile

    tree = tmp_path / "archive-src" / root
    tree.mkdir(parents=True)
    (tree / "SOUL.md").write_text("a" * soul_chars, encoding="utf-8")
    (tree / "config.yaml").write_text("model:\n  model: gpt-4\n")
    archive = tmp_path / f"{root}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(tree, arcname=root)
    return archive


def test_import_refuses_soul_over_limit_and_leaves_no_profile_dir(profile_env, tmp_path):
    from hermes_cli.profiles import import_profile

    over = SOUL_CHAR_LIMIT + 1
    archive = _make_profile_archive(tmp_path, "imp", over)
    with pytest.raises(ValueError) as excinfo:
        import_profile(str(archive))
    _assert_refused(excinfo, over)
    assert str(archive) in str(excinfo.value)
    assert not get_profile_dir("imp").exists()


def test_import_allows_soul_exactly_at_limit(profile_env, tmp_path):
    from hermes_cli.profiles import import_profile

    archive = _make_profile_archive(tmp_path, "imp", SOUL_CHAR_LIMIT)
    dst = import_profile(str(archive), name="edge")
    assert dst == get_profile_dir("edge")
    assert len((dst / "SOUL.md").read_text(encoding="utf-8")) == SOUL_CHAR_LIMIT


def test_distribution_install_without_soul_seeds_default(profile_env, tmp_path):
    from hermes_cli.default_soul import DEFAULT_SOUL_MD
    from hermes_cli.profile_distribution import (
        DistributionManifest,
        install_distribution,
        write_manifest,
    )

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "config.yaml").write_text("model:\n  model: gpt-4\n")
    write_manifest(staged, DistributionManifest(name="dist", version="0.1.0"))
    plan = install_distribution(str(staged), name="dist")
    assert (plan.target_dir / "SOUL.md").read_text(encoding="utf-8") == DEFAULT_SOUL_MD

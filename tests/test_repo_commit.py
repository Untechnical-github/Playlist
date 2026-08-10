import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from core.repo_commit import CommitError, commit_and_push


def _run(args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"{args} failed: {result.stderr}"
    return result


def _init_remote_and_clone(tmp_path):
    """bare remoteと、そこからcloneしたworking treeを用意する（テスト用の使い捨てリポジトリ）。"""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run(["git", "init", "--bare"], cwd=remote)

    clone = tmp_path / "clone"
    _run(["git", "clone", str(remote), str(clone)], cwd=tmp_path)
    _run(["git", "-c", "user.name=tester", "-c", "user.email=tester@example.com", "commit", "--allow-empty", "-m", "init"], cwd=clone)
    _run(["git", "push"], cwd=clone)
    return remote, clone


def test_commit_and_push_returns_false_when_no_changes(tmp_path, monkeypatch):
    _remote, clone = _init_remote_and_clone(tmp_path)
    monkeypatch.chdir(clone)

    (clone / "data.json").write_text("{}", encoding="utf-8")
    _run(["git", "add", "data.json"], cwd=clone)
    _run(["git", "-c", "user.name=tester", "-c", "user.email=tester@example.com", "commit", "-m", "add data.json"], cwd=clone)
    _run(["git", "push"], cwd=clone)

    assert commit_and_push(["data.json"], "no-op commit") is False


def test_commit_and_push_commits_and_pushes_changes(tmp_path, monkeypatch):
    remote, clone = _init_remote_and_clone(tmp_path)
    (clone / "data.json").write_text("{}", encoding="utf-8")
    _run(["git", "add", "data.json"], cwd=clone)
    _run(["git", "-c", "user.name=tester", "-c", "user.email=tester@example.com", "commit", "-m", "add data.json"], cwd=clone)
    _run(["git", "push"], cwd=clone)

    monkeypatch.chdir(clone)
    (clone / "data.json").write_text('{"a": 1}', encoding="utf-8")

    assert commit_and_push(["data.json"], "update data.json") is True

    verify = tmp_path / "verify"
    _run(["git", "clone", str(remote), str(verify)], cwd=tmp_path)
    assert (verify / "data.json").read_text(encoding="utf-8") == '{"a": 1}'
    log = _run(["git", "log", "-1", "--pretty=%an %ae %s"], cwd=verify)
    assert "update data.json" in log.stdout
    assert "playlist-bot" in log.stdout


def test_commit_and_push_rebases_and_retries_on_push_conflict(tmp_path, monkeypatch):
    remote, clone_a = _init_remote_and_clone(tmp_path)
    (clone_a / "data.json").write_text("{}", encoding="utf-8")
    _run(["git", "add", "data.json"], cwd=clone_a)
    _run(["git", "-c", "user.name=tester", "-c", "user.email=tester@example.com", "commit", "-m", "add data.json"], cwd=clone_a)
    _run(["git", "push"], cwd=clone_a)

    clone_b = tmp_path / "clone_b"
    _run(["git", "clone", str(remote), str(clone_b)], cwd=tmp_path)

    # clone_bが先にpushして、clone_aのローカルはリモートより遅れた状態にする
    (clone_b / "other.json").write_text("{}", encoding="utf-8")
    _run(["git", "add", "other.json"], cwd=clone_b)
    _run(["git", "-c", "user.name=tester", "-c", "user.email=tester@example.com", "commit", "-m", "add other.json"], cwd=clone_b)
    _run(["git", "push"], cwd=clone_b)

    monkeypatch.chdir(clone_a)
    (clone_a / "data.json").write_text('{"a": 1}', encoding="utf-8")

    assert commit_and_push(["data.json"], "update data.json") is True

    verify = tmp_path / "verify"
    _run(["git", "clone", str(remote), str(verify)], cwd=tmp_path)
    assert (verify / "data.json").read_text(encoding="utf-8") == '{"a": 1}'
    assert (verify / "other.json").exists()  # clone_bの変更も残っている


def test_commit_and_push_raises_when_push_keeps_failing(tmp_path, monkeypatch):
    _remote, clone = _init_remote_and_clone(tmp_path)
    monkeypatch.chdir(clone)
    (clone / "data.json").write_text("{}", encoding="utf-8")

    # リモートを消してpushが常に失敗する状況を作る
    _run(["git", "remote", "remove", "origin"], cwd=clone)
    _run(["git", "remote", "add", "origin", "https://invalid.invalid/does-not-exist.git"], cwd=clone)

    with pytest.raises(CommitError):
        commit_and_push(["data.json"], "add data.json", max_retries=1)

import os
import subprocess
from typing import Sequence

GIT_BOT_NAME = os.environ.get("GIT_BOT_NAME", "playlist-bot")
GIT_BOT_EMAIL = os.environ.get("GIT_BOT_EMAIL", "playlist-bot@users.noreply.github.com")


class CommitError(Exception):
    """git操作（add/commit/push/pull）が失敗したときに送出する。"""


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8")


def commit_and_push(paths: Sequence[str], message: str, max_retries: int = 3) -> bool:
    """指定したファイルの変更をコミットしてpushする。差分が無ければ何もせずFalseを返す。

    pushが他のジョブとのコンフリクトで失敗した場合は、`git pull --rebase`してから
    `max_retries`回まで再試行する（GitHub Actionsから複数ジョブが並行して同じファイルに
    コミットする可能性があるため）。
    """
    add_result = _run(["git", "add", *paths])
    if add_result.returncode != 0:
        raise CommitError(f"git add failed: {add_result.stderr}")

    diff_result = _run(["git", "diff", "--cached", "--quiet"])
    if diff_result.returncode == 0:
        return False  # 差分なし

    commit_result = _run(
        [
            "git",
            "-c",
            f"user.name={GIT_BOT_NAME}",
            "-c",
            f"user.email={GIT_BOT_EMAIL}",
            "commit",
            "-m",
            message,
        ]
    )
    if commit_result.returncode != 0:
        raise CommitError(f"git commit failed: {commit_result.stderr}")

    for attempt in range(1, max_retries + 1):
        push_result = _run(["git", "push"])
        if push_result.returncode == 0:
            return True
        if attempt == max_retries:
            raise CommitError(f"git push failed after {max_retries} attempts: {push_result.stderr}")
        pull_result = _run(["git", "pull", "--rebase"])
        if pull_result.returncode != 0:
            raise CommitError(f"git pull --rebase failed: {pull_result.stderr}")
    return True

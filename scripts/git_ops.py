"""
Git operations for Canon CLI.

Reads and writes to the Canon repo via the GitHub REST API.
Auth: GITHUB_TOKEN (Actions built-in) or CANON_GITHUB_TOKEN (local dev / PAT).
Repo slug: GITHUB_REPOSITORY (Actions) or CANON_GITHUB_REPO (local dev).
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_REPO_SLUG = os.environ.get(
    "GITHUB_REPOSITORY",
    os.environ.get("CANON_GITHUB_REPO", ""),
)
_GITHUB_TOKEN = os.environ.get(
    "GITHUB_TOKEN",
    os.environ.get("CANON_GITHUB_TOKEN", ""),
)


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("CANON_GITHUB_TOKEN", "")
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo() -> str:
    slug = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("CANON_GITHUB_REPO", "")
    if not slug:
        raise RuntimeError(
            "GitHub repo slug not set. Set GITHUB_REPOSITORY (Actions) "
            "or CANON_GITHUB_REPO (local dev)."
        )
    return slug


@dataclass
class FileContent:
    path: str
    content: str
    sha: str


def get_file(path: str, ref: str = "main") -> FileContent:
    """Fetch a file from the Canon repo."""
    url = f"{_GITHUB_API}/repos/{_repo()}/contents/{path}?ref={ref}"
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return FileContent(path=path, content=content, sha=data["sha"])


def create_branch(branch_name: str, base_ref: str = "main") -> str:
    """Create a new branch from base_ref. Returns the base SHA."""
    ref_url = f"{_GITHUB_API}/repos/{_repo()}/git/ref/heads/{base_ref}"
    resp = requests.get(ref_url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    base_sha = resp.json()["object"]["sha"]

    create_url = f"{_GITHUB_API}/repos/{_repo()}/git/refs"
    resp = requests.post(
        create_url,
        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code == 422:
        logger.info("Branch '%s' already exists", branch_name)
    else:
        resp.raise_for_status()

    return base_sha


def commit_file(
    path: str,
    content: str,
    message: str,
    branch: str,
    sha: str | None = None,
) -> dict:
    """Commit a file to a branch. sha is required for updates."""
    url = f"{_GITHUB_API}/repos/{_repo()}/contents/{path}"
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload: dict = {
        "message": message,
        "content": encoded,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(url, json=payload, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def open_pr(
    title: str,
    body: str,
    head_branch: str,
    base_branch: str = "main",
    draft: bool = True,
    labels: list[str] | None = None,
) -> dict:
    """Open a pull request (draft by default)."""
    url = f"{_GITHUB_API}/repos/{_repo()}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
        "draft": draft,
    }
    resp = requests.post(url, json=payload, headers=_headers(), timeout=15)
    resp.raise_for_status()
    pr = resp.json()

    if labels:
        issues_url = f"{_GITHUB_API}/repos/{_repo()}/issues/{pr['number']}/labels"
        requests.post(issues_url, json={"labels": labels}, headers=_headers(), timeout=15)

    return pr


def get_open_issues(labels: list[str]) -> list[dict]:
    """Get open issues with the specified labels."""
    label_str = ",".join(labels)
    url = f"{_GITHUB_API}/repos/{_repo()}/issues?labels={label_str}&state=open&per_page=10"
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def close_issue(issue_number: int, comment: str = "") -> None:
    """Close a GitHub issue, optionally with a comment."""
    if comment:
        requests.post(
            f"{_GITHUB_API}/repos/{_repo()}/issues/{issue_number}/comments",
            json={"body": comment},
            headers=_headers(),
            timeout=15,
        )
    requests.patch(
        f"{_GITHUB_API}/repos/{_repo()}/issues/{issue_number}",
        json={"state": "closed"},
        headers=_headers(),
        timeout=15,
    )

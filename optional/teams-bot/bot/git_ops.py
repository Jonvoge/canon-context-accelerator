"""
Git operations for the Canon bot.

The bot reads and writes to the Canon repo via the GitHub REST API
using a Personal Access Token (CANON_GITHUB_TOKEN) or GitHub App.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_REPO_SLUG = os.environ.get("CANON_GITHUB_REPO", "Jonvoge/canon-context-accelerator")
_GITHUB_TOKEN = os.environ.get("CANON_GITHUB_TOKEN", "")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"token {_GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


@dataclass
class FileContent:
    path: str
    content: str
    sha: str


def get_file(path: str, ref: str = "main") -> FileContent:
    """Fetch a file from the Canon repo."""
    url = f"{_GITHUB_API}/repos/{_REPO_SLUG}/contents/{path}?ref={ref}"
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return FileContent(path=path, content=content, sha=data["sha"])


def create_branch(branch_name: str, base_ref: str = "main") -> str:
    """Create a new branch from base_ref. Returns the new branch's SHA."""
    # Get base SHA
    ref_url = f"{_GITHUB_API}/repos/{_REPO_SLUG}/git/ref/heads/{base_ref}"
    resp = requests.get(ref_url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    base_sha = resp.json()["object"]["sha"]

    # Create branch
    create_url = f"{_GITHUB_API}/repos/{_REPO_SLUG}/git/refs"
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
    url = f"{_GITHUB_API}/repos/{_REPO_SLUG}/contents/{path}"
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
    labels: list[str] | None = None,
) -> dict:
    """Open a pull request."""
    url = f"{_GITHUB_API}/repos/{_REPO_SLUG}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
    }
    resp = requests.post(url, json=payload, headers=_headers(), timeout=15)
    resp.raise_for_status()
    pr = resp.json()

    if labels:
        # Add labels
        issues_url = f"{_GITHUB_API}/repos/{_REPO_SLUG}/issues/{pr['number']}/labels"
        requests.post(issues_url, json={"labels": labels}, headers=_headers(), timeout=15)

    return pr


def get_open_issues(labels: list[str]) -> list[dict]:
    """Get open issues with the specified labels."""
    label_str = ",".join(labels)
    url = f"{_GITHUB_API}/repos/{_REPO_SLUG}/issues?labels={label_str}&state=open&per_page=10"
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def close_issue(issue_number: int, comment: str = "") -> None:
    """Close a GitHub issue, optionally with a comment."""
    if comment:
        requests.post(
            f"{_GITHUB_API}/repos/{_REPO_SLUG}/issues/{issue_number}/comments",
            json={"body": comment},
            headers=_headers(),
            timeout=15,
        )
    requests.patch(
        f"{_GITHUB_API}/repos/{_REPO_SLUG}/issues/{issue_number}",
        json={"state": "closed"},
        headers=_headers(),
        timeout=15,
    )

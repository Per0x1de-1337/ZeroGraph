"""Detect primary programming language for ZeroGraph CPG indexing."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
import json
from collections import Counter
from pathlib import Path
from zerograph.defaults import SUPPORTED_LANGUAGES

# Extension → ZeroGraph language id
_EXT_LANG: dict[str, str] = {
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".py": "python",
    ".go": "go",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
}

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "vendor",
    "venv",
    ".venv",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".gradle",
    "playground",
    "workspace",
}

_GITHUB_LANG: dict[str, str] = {
    "c": "c",
    "c++": "cpp",
    "java": "java",
    "javascript": "javascript",
    "typescript": "javascript",
    "python": "python",
    "go": "go",
    "kotlin": "kotlin",
    "c#": "csharp",
    "php": "php",
    "ruby": "ruby",
    "swift": "swift",
}

_MAX_WALK_FILES = 25_000


def _is_github_url(repo: str) -> bool:
    return repo.startswith("https://github.com/") or repo.startswith("http://github.com/")


def _parse_github_repo(repo: str) -> tuple[str, str] | None:
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?", repo.strip())
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    return owner, name.rstrip("/")


def _count_extensions(root: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    walked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            walked += 1
            if walked > _MAX_WALK_FILES:
                return counts
            ext = Path(fname).suffix.lower()
            lang = _EXT_LANG.get(ext)
            if lang:
                counts[lang] += 1
    return counts


def _pick_language(counts: Counter[str]) -> str:
    if not counts:
        return "c"
    # Merge c/cpp if both present — prefer cpp when C++ sources dominate
    c_count = counts.get("c", 0)
    cpp_count = counts.get("cpp", 0)
    if c_count or cpp_count:
        if cpp_count >= c_count:
            counts["cpp"] += counts.pop("c", 0)
        else:
            counts["c"] += counts.pop("cpp", 0)
    lang, _ = counts.most_common(1)[0]
    if lang not in SUPPORTED_LANGUAGES:
        return "c"
    return lang


def _github_languages_api(owner: str, repo: str, token: str | None) -> Counter[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/languages"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "zerograph-agent"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return Counter()
    counts: Counter[str] = Counter()
    for gh_name, bytes_count in data.items():
        mapped = _GITHUB_LANG.get(gh_name.lower())
        if mapped:
            counts[mapped] += int(bytes_count)
    return counts


def detect_language(repo: str, *, github_token: str | None = None) -> str:
    """
    Return a ZeroGraph-supported language id for *repo* (GitHub URL or local path).
    """
    if _is_github_url(repo):
        parsed = _parse_github_repo(repo)
        if parsed:
            owner, name = parsed
            gh_counts = _github_languages_api(owner, name, github_token)
            if gh_counts:
                return _pick_language(gh_counts)

    path = Path(repo).expanduser()
    if path.is_dir():
        return _pick_language(_count_extensions(path.resolve()))

    return "c"

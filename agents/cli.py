"""Minimal CLI helpers for agents."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone


def default_report_path(prefix: str, suffix: str = ".md") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}{suffix}"


def add_repo_output(p: argparse.ArgumentParser, *, output_default: str) -> None:
    p.add_argument("repo", help="GitHub URL or path to source tree")
    p.add_argument(
        "-o",
        "--output",
        default=os.getenv("ZEROGRAPH_REPORT", "") or output_default,
        help="Report file (default: auto-named in cwd)",
    )


def env_mcp_url() -> str:
    return os.getenv("ZEROGRAPH_MCP_URL", "http://localhost:4242/mcp")


def env_github_token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")


def env_openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-5.5")


def env_max_turns() -> int:
    raw = os.getenv("ZEROGRAPH_AGENT_MAX_TURNS", "35")
    try:
        return max(5, int(raw))
    except ValueError:
        return 35

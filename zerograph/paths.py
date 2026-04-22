"""Filesystem layout for host and container workspaces."""

from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PKG_ROOT.parent

WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
REPOS_DIR = WORKSPACE_ROOT / "repos"
GRAPHS_DIR = WORKSPACE_ROOT / "graphs"
FIXTURES_DIR = PROJECT_ROOT / "fixtures"

CONTAINER_WS = "/var/zerograph/ws"
CONTAINER_REPOS = f"{CONTAINER_WS}/repos"
CONTAINER_GRAPHS = f"{CONTAINER_WS}/graphs"


def host_repos(hash_id: str) -> Path:
    return REPOS_DIR / hash_id


def host_graph(hash_id: str) -> Path:
    return GRAPHS_DIR / hash_id / "graph.bin"


def container_repo(hash_id: str) -> str:
    return f"{CONTAINER_REPOS}/{hash_id}"


def container_graph(hash_id: str) -> str:
    return f"{CONTAINER_GRAPHS}/{hash_id}/graph.bin"


def host_to_container(host_path: str) -> str | None:
    marker = f"{WORKSPACE_ROOT.as_posix()}/"
    if marker in host_path.replace("\\", "/"):
        rel = host_path.split(marker, 1)[-1]
        return f"{CONTAINER_WS}/{rel}"
    return None

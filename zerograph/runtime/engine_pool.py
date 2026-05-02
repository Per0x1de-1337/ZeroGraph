"""CPG engine manager — per-codebase analysis runtimes inside Docker."""

import asyncio
import logging
import time
import os
from collections import OrderedDict
from typing import Dict, Optional, TYPE_CHECKING

import docker
from docker.errors import DockerException, NotFound, APIError

from .port_registry import PortRegistry

if TYPE_CHECKING:
    from zerograph.runtime.catalog import Catalog

logger = logging.getLogger(__name__)

class EnginePool:
    """Manages individual analysis engine instances running in Docker container using Docker Python API"""

    def __init__(
        self,
        engine_binary_path: str = "cpg-parse",
        container_name: str = "zg-runtime",
        config=None,
        codebase_tracker: Optional["Catalog"] = None,
        max_active_servers: Optional[int] = None,
    ):
        self.engine_binary = engine_binary_path
        self.container_name = container_name
        self.config = config
        self.codebase_tracker = codebase_tracker
        if config:
            self.port_manager = PortRegistry(port_min=config.engine.port_min, port_max=config.engine.port_max)
        else:
            self.port_manager = PortRegistry()
        self.docker_client = docker.from_env()
        self._exec_ids: Dict[str, str] = {}
        self._ports: Dict[str, int] = {}
        self._clients: Dict[str, "EngineRpc"] = {}

        # LRU pool
        if max_active_servers is not None:
            self._max_active = max_active_servers
        elif config:
            self._max_active = config.engine.max_active_servers
        else:
            self._max_active = 3
        self._lru: OrderedDict[str, None] = OrderedDict()
        self._lru_eviction_count: int = 0

        self._watchdog_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ LRU

    def _touch(self, codebase_hash: str) -> None:
        self._lru.pop(codebase_hash, None)
        self._lru[codebase_hash] = None

    def _container_memory_mb(self) -> float:
        """Return the Docker container's current RSS in MB (0.0 on any error)."""
        try:
            container = self.docker_client.containers.get(self.container_name)
            stats = container.stats(stream=False)
            usage = stats.get("memory_stats", {}).get("usage", 0)
            return usage / (1024 * 1024)
        except Exception:
            return 0.0

    def _evict_lru_if_needed(self) -> Optional[str]:
        # Count-based eviction (existing)
        if len(self._ports) < self._max_active:
            # Also check RSS pressure even when under the server count limit.
            rss_mb = self._container_memory_mb()
            rss_limit_mb = (
                self.config.engine.rss_eviction_threshold_mb if (
                    self.config and hasattr(self.config.engine, "rss_eviction_threshold_mb")
                ) else 0
            )
            if rss_limit_mb <= 0 or rss_mb < rss_limit_mb:
                return None
            logger.warning(
                f"Container RSS {rss_mb:.0f} MB exceeds threshold {rss_limit_mb} MB — "
                f"evicting LRU server under memory pressure"
            )
        if not self._lru:
            return None
        lru_hash, _ = next(iter(self._lru.items()))
        logger.info(f"Evicting LRU analysis engine: {lru_hash}")
        self.terminate_server(lru_hash)
        self._lru.pop(lru_hash, None)
        self._lru_eviction_count += 1
        if self.codebase_tracker:
            try:
                self.codebase_tracker.update_codebase(
                    lru_hash,
                    engine_port=None,
                    metadata={"status": "sleeping"},
                )
            except Exception as e:
                logger.warning(f"Failed to update sleeping status for {lru_hash}: {e}")
        return lru_hash

    # ----------------------------------------------------------- spawn / load

    def spawn_server(self, codebase_hash: str) -> int:
        try:
            if codebase_hash in self._ports:
                port = self._ports[codebase_hash]
                if self.is_server_running(codebase_hash):
                    self._touch(codebase_hash)
                    return port
                else:
                    logger.warning(f"Server for {codebase_hash} registered but not running, cleaning up")
                    self._cleanup_server(codebase_hash)
                    self._lru.pop(codebase_hash, None)

            self._evict_lru_if_needed()

            port = self.port_manager.allocate_port(codebase_hash)

            try:
                container = self.docker_client.containers.get(self.container_name)
            except NotFound:
                logger.error(f"Container {self.container_name} not found")
                self.port_manager.release_port(codebase_hash)
                raise RuntimeError(f"Container {self.container_name} not found")

            # Ensure no stale JVM is still holding the port before we try to bind it.
            # This closes the race where terminate_server releases the port in our
            # state but the SIGTERM'd JVM hasn't exited yet.
            self._ensure_port_free(container, port)

            work_dir = f"/tmp/zg-runtime-{codebase_hash}"
            log_file = f"/tmp/zg-runtime-{codebase_hash}.log"

            java_opts = self.config.engine.java_opts if self.config else ""
            java_opts_export = f"export JAVA_OPTS='{java_opts}' && " if java_opts else ""

            engine_cmd = [
                "bash", "-c",
                f"{java_opts_export}mkdir -p '{work_dir}' && cd '{work_dir}' && nohup /opt/zg-runtime/bin/cpg-server --server --server-host 0.0.0.0 --server-port {port} > '{log_file}' 2>&1 &"
            ]

            logger.info(f"Starting analysis engine for {codebase_hash} on port {port} inside container {self.container_name}")

            container.exec_run(cmd=engine_cmd, detach=True, stream=False)

            self._exec_ids[codebase_hash] = f"exec-{codebase_hash}"
            self._ports[codebase_hash] = port

            host = self.config.engine.server_host if self.config else "localhost"
            logger.info(f"analysis engine command executed, waiting for server to be ready at {host}:{port}...")

            startup_timeout = self.config.engine.server_startup_timeout if self.config else 120
            if self._wait_for_server(port, timeout=startup_timeout):
                self._touch(codebase_hash)
                logger.info(f"analysis engine for {codebase_hash} started successfully on port {port}")
                return port
            else:
                logger.error(f"analysis engine for {codebase_hash} failed to become ready on port {port}")
                try:
                    log_result = container.exec_run(cmd=["cat", log_file], stream=False)
                    if log_result.exit_code == 0:
                        logger.error(f"analysis engine log:\n{log_result.output.decode('utf-8')}")
                except Exception as log_error:
                    logger.warning(f"Could not read log file: {log_error}")
                self._cleanup_server(codebase_hash)
                raise RuntimeError(f"analysis engine for {codebase_hash} failed to start on port {port}")

        except DockerException as e:
            logger.error(f"Docker error while spawning analysis engine for {codebase_hash}: {e}", exc_info=True)
            self._cleanup_server(codebase_hash)
            raise
        except Exception as e:
            logger.error(f"Failed to spawn analysis engine for {codebase_hash}: {e}", exc_info=True)
            self._cleanup_server(codebase_hash)
            raise

    def reactivate(self, codebase_hash: str, cpg_path: str) -> int:
        """Spawn a fresh analysis engine process and load the existing CPG binary (no regeneration)."""
        logger.info(f"Reactivating sleeping codebase {codebase_hash}")
        port = self.spawn_server(codebase_hash)
        self.load_cpg(codebase_hash, cpg_path)
        if self.codebase_tracker:
            try:
                self.codebase_tracker.update_codebase(
                    codebase_hash,
                    engine_port=port,
                    metadata={"status": "ready"},
                )
            except Exception as e:
                logger.warning(f"Failed to update ready status for {codebase_hash}: {e}")
        return port

    def get_or_create_client(self, codebase_hash: str) -> "EngineRpc":
        if codebase_hash in self._clients:
            self._touch(codebase_hash)
            return self._clients[codebase_hash]

        if codebase_hash not in self._ports:
            raise RuntimeError(f"No analysis engine running for codebase {codebase_hash}")

        port = self._ports[codebase_hash]

        from .engine_rpc import EngineRpc

        http_config = {}
        if self.config:
            engine_cfg = self.config.engine
            http_config = {
                "http_pool_connections": engine_cfg.http_pool_connections,
                "http_pool_maxsize": engine_cfg.http_pool_maxsize,
                "http_max_retries": engine_cfg.http_max_retries,
                "http_backoff_factor": engine_cfg.http_backoff_factor,
            }

        client = EngineRpc(
            host=self.config.engine.server_host if self.config else "localhost",
            port=port,
            username=self.config.engine.server_auth_username if self.config else None,
            password=self.config.engine.server_auth_password if self.config else None,
            config=http_config,
        )

        self._clients[codebase_hash] = client
        self._touch(codebase_hash)
        logger.debug(f"Created and cached EngineRpc for {codebase_hash} on port {port}")
        return client

    def load_cpg(self, codebase_hash: str, cpg_path: str, timeout: int = 0) -> bool:
        """Load CPG into analysis engine.

        importCpg triggers expensive overlay computation (ReachingDefPass, dataflow).
        On timeout or failure the server is terminated so the spinning JVM doesn't
        linger at 100% CPU.  We do NOT retry — a timeout means the JVM is stuck,
        not that there was a transient network hiccup.
        """
        if timeout == 0:
            timeout = self.config.engine.cpg_load_timeout if self.config else 300

        try:
            if codebase_hash not in self._ports:
                raise RuntimeError(f"No analysis engine running for codebase {codebase_hash}")

            port = self._ports[codebase_hash]
            client = self.get_or_create_client(codebase_hash)

            container_cpg_path = cpg_path
            if "/var/zerograph/ws/" in cpg_path:
                parts = cpg_path.split("/var/zerograph/ws/")
                if len(parts) >= 2:
                    container_cpg_path = f"/var/zerograph/ws/{parts[-1]}"

            logger.info(
                f"Loading CPG {cpg_path} (container: {container_cpg_path}) "
                f"into analysis engine for {codebase_hash} (port {port}, timeout {timeout}s)"
            )

            success = client.load_cpg(container_cpg_path, project_name=codebase_hash, timeout=timeout)

            if success:
                logger.info(f"CPG loaded successfully for {codebase_hash}")
                return True

            # importCpg failed or timed out — kill the server so the JVM doesn't
            # spin forever at 100% CPU doing overlay computation.
            logger.error(
                f"Failed to load CPG for {codebase_hash} (timeout={timeout}s) — "
                f"terminating server to stop stuck overlay computation"
            )
            self.terminate_server(codebase_hash)
            return False

        except Exception as e:
            logger.error(f"Error loading CPG for {codebase_hash}: {e}")
            self.terminate_server(codebase_hash)
            return False

    def get_server_port(self, codebase_hash: str) -> Optional[int]:
        return self._ports.get(codebase_hash)

    def is_server_running(self, codebase_hash: str) -> bool:
        if codebase_hash not in self._ports:
            return False
        port = self._ports[codebase_hash]
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            host = self.config.engine.server_host if self.config else "localhost"
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception as e:
            logger.debug(f"Failed to check server status for {codebase_hash} on port {port}: {e}")
            return False

    def terminate_server(self, codebase_hash: str) -> bool:
        try:
            if codebase_hash not in self._exec_ids:
                logger.warning(f"No server found for codebase {codebase_hash}")
                return False

            port = self._ports.get(codebase_hash)
            logger.info(f"Terminating analysis engine for {codebase_hash} on port {port}")

            try:
                container = self.docker_client.containers.get(self.container_name)
                kill_cmd = ["bash", "-c",
                    f"pkill -f 'cpg-server.*--server-port {port}' || true; "
                    f"sleep 3; pkill -9 -f 'cpg-server.*--server-port {port}' || true"]
                container.exec_run(cmd=kill_cmd)
            except Exception as e:
                logger.warning(f"Error killing analysis engine process: {e}")

            self._cleanup_server(codebase_hash)
            return True

        except Exception as e:
            logger.error(f"Error terminating analysis engine for {codebase_hash}: {e}")
            return False

    def terminate_all_servers(self) -> None:
        logger.info("Terminating all analysis engines")
        codebases = list(self._exec_ids.keys())
        for codebase_hash in codebases:
            self.terminate_server(codebase_hash)
        logger.info("All analysis engines terminated")

    def get_running_servers(self) -> Dict[str, int]:
        return {
            h: p for h, p in self._ports.items()
            if self.is_server_running(h)
        }

    # ---------------------------------------------------------- watchdog (C1)

    def start_watchdog(self) -> None:
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info("analysis engine watchdog started")

    async def _watchdog_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                for codebase_hash, port in list(self._ports.items()):
                    if not await self._is_server_healthy(port):
                        logger.warning(f"analysis engine {codebase_hash}:{port} is dead, respawning")
                        self.terminate_server(codebase_hash)
                        asyncio.create_task(self._respawn_server(codebase_hash))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Watchdog loop error: {e}", exc_info=True)

    async def _is_server_healthy(self, port: int) -> bool:
        host = self.config.engine.server_host if self.config else "localhost"
        import requests as _requests
        loop = asyncio.get_running_loop()
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: _requests.get(f"http://{host}:{port}", timeout=5),
                ),
                timeout=8,
            )
            return response.status_code in [200, 404]
        except Exception:
            return False

    async def _respawn_server(self, codebase_hash: str) -> None:
        if not self.codebase_tracker:
            return
        try:
            info = self.codebase_tracker.get_codebase(codebase_hash)
            if not info or not info.cpg_path:
                logger.warning(f"Cannot respawn {codebase_hash}: no CPG path found")
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.reactivate, codebase_hash, info.cpg_path)
            logger.info(f"Watchdog: respawned server for {codebase_hash}")
        except Exception as e:
            logger.error(f"Watchdog: failed to respawn {codebase_hash}: {e}", exc_info=True)

    # ----------------------------------------------------------- internal helpers

    def _wait_for_server(self, port: int, timeout: int = 30) -> bool:
        import requests
        host = self.config.engine.server_host if self.config else "localhost"
        url = f"http://{host}:{port}"
        deadline = time.time() + timeout

        # Poll until the HTTP server responds. We don't do a prior TCP-only check
        # because a pre-existing stale JVM could make the port look "open" before
        # our freshly spawned process has even started.
        while time.time() < deadline:
            try:
                response = requests.get(url, timeout=2)
                if response.status_code in [200, 404]:
                    sleep_time = self.config.engine.server_init_sleep_time if self.config else 3.0
                    time.sleep(sleep_time)
                    return True
            except Exception as e:
                logger.debug(f"HTTP check on :{port} failed: {e}")
            time.sleep(1)

        return False

    def _ensure_port_free(self, container, port: int, wait: int = 8) -> None:
        """Kill any process still holding *port* inside the container, then wait for it to close."""
        import socket
        host = self.config.engine.server_host if self.config else "localhost"

        def _port_open() -> bool:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                result = s.connect_ex((host, port))
                s.close()
                return result == 0
            except Exception:
                return False

        if not _port_open():
            return  # Nothing to do

        logger.warning(f"Port {port} still in use before spawn — force-killing stale process")
        try:
            # pkill by name only kills the shell wrapper (e.g. cpg-server bash script);
            # the JVM child it spawned survives as an orphan and keeps holding the port.
            # Kill by TCP port directly so we hit the actual process that owns the socket.
            # fuser and lsof cover different distros; the ||true suppresses "no process" exits.
            container.exec_run(
                cmd=["bash", "-c",
                     f"fuser -k {port}/tcp 2>/dev/null; "
                     f"lsof -ti :{port} 2>/dev/null | xargs -r kill -9 2>/dev/null; "
                     f"true"],
                stream=False,
            )
        except Exception as e:
            logger.warning(f"Error force-killing process on port {port}: {e}")

        deadline = time.time() + 20
        while time.time() < deadline:
            if not _port_open():
                logger.info(f"Port {port} is now free")
                return
            time.sleep(0.5)

        logger.error(f"Port {port} still occupied after 20s — spawn may fail with BindException")

    def _cleanup_server(self, codebase_hash: str) -> None:
        if codebase_hash in self._exec_ids:
            del self._exec_ids[codebase_hash]
        if codebase_hash in self._ports:
            port = self._ports[codebase_hash]
            self.port_manager.release_port(codebase_hash)
            del self._ports[codebase_hash]
            logger.debug(f"Cleaned up resources for {codebase_hash} (port {port})")
        if codebase_hash in self._clients:
            client = self._clients[codebase_hash]
            try:
                client.close()
            except Exception as e:
                logger.warning(f"Error closing HTTP session for {codebase_hash}: {e}")
            del self._clients[codebase_hash]
        self._lru.pop(codebase_hash, None)

"""
CPG Generator for creating Code Property Graphs using CPG toolchain
"""

import asyncio
import logging
import os
import re
import subprocess
from typing import AsyncIterator, Dict, Optional

from ..exceptions import CPGGenerationError
from ..models import CPGConfig, Config
from ..telemetry import get_tracer
from .engine_rpc import EngineRpc

logger = logging.getLogger(__name__)
tracer = get_tracer()


class GraphBuilder:
    """Generates CPG from source code using Docker containers"""

    # Language-specific language importer commands (full paths inside container)
    LANGUAGE_COMMANDS = {
        "java": "/opt/zg-runtime/bin/javasrc2cpg",
        "c": "/opt/zg-runtime/bin/c2cpg.sh",
        "cpp": "/opt/zg-runtime/bin/c2cpg.sh",
        "javascript": "/opt/zg-runtime/bin/jssrc2cpg.sh",
        "python": "/opt/zg-runtime/bin/pysrc2cpg",
        "go": "/opt/zg-runtime/bin/gosrc2cpg",
        "kotlin": "/opt/zg-runtime/bin/kotlin2cpg",
        "csharp": "/opt/zg-runtime/bin/csharpsrc2cpg",
        "ghidra": "/opt/zg-runtime/bin/ghidra2cpg",
        "jimple": "/opt/zg-runtime/bin/jimple2cpg",
        "php": "/opt/zg-runtime/bin/php2cpg",
        "ruby": "/opt/zg-runtime/bin/rubysrc2cpg",
        "swift": "/opt/zg-runtime/bin/swiftsrc2cpg.sh",
    }

    def __init__(
        self, config: Config, engine_pool: Optional['EnginePool'] = None, docker_orchestrator=None
    ):
        self.config = config
        self.engine_pool = engine_pool
        # docker_orchestrator is ignored - we run CPG toolchain directly

    def initialize(self):
        """Initialize CPG Generator (no-op in container)"""
        logger.info("CPG Generator initialized (running locally)")

    def build_graph(
        self, source_path: str, language: str, cpg_path: str, codebase_hash: str
    ) -> tuple[str, Optional[int]]:
        """Generate CPG from source code using CPG toolchain inside Docker container

        Args:
            source_path: Host path to source code (e.g., /home/aleks/.../var/zerograph/ws/repos/<hash>/)
            language: Programming language
            cpg_path: Host path where CPG should be stored (e.g., /home/aleks/.../var/zerograph/ws/graphs/<hash>/graph.bin)
            codebase_hash: The codebase identifier for server management

        Returns:
            Tuple of (host path to generated CPG file, engine port or None)
        """
        with tracer.start_as_current_span("cpg.generate") as span:
            span.set_attribute("cpg.language", language)
            span.set_attribute("cpg.codebase_hash", codebase_hash)
            span.set_attribute("cpg.source_path", source_path)

            try:
                logger.info(f"Starting CPG generation for {source_path} -> {cpg_path}")

                # Get language-specific command
                if language not in self.LANGUAGE_COMMANDS:
                    raise CPGGenerationError(f"Unsupported language: {language}")

                base_cmd = self.LANGUAGE_COMMANDS[language]

                # Create CPG directory on host (we can do this from host)
                cpg_dir = os.path.dirname(cpg_path)
                os.makedirs(cpg_dir, exist_ok=True)
                logger.info(f"CPG directory created: {cpg_dir}")

                # Validate repository size before CPG generation
                repo_size_mb = self._calculate_repo_size_mb(source_path)
                max_size_mb = self.config.cpg.max_repo_size_mb
                span.set_attribute("cpg.repo_size_mb", repo_size_mb)
                logger.info(f"Repository size: {repo_size_mb}MB, max allowed: {max_size_mb}MB")

                if repo_size_mb > max_size_mb:
                    error_msg = (
                        f"Repository size ({repo_size_mb}MB) exceeds maximum allowed "
                        f"({max_size_mb}MB). Please reduce the repository size or increase "
                        f"the max_repo_size_mb configuration."
                    )
                    logger.error(error_msg)
                    raise CPGGenerationError(error_msg)

                # Convert host paths to container paths for the analysis engine to use
                # Host path like /home/aleks/.../var/zerograph/ws/repos/hash -> /var/zerograph/ws/repos/hash
                container_source_path = self._host_to_container_path(source_path)
                container_cpg_path = self._host_to_container_path(cpg_path)

                logger.info(f"Container paths: src={container_source_path}, cpg={container_cpg_path}")

                # Get Java opts from config
                java_opts = self.config.engine.java_opts or "-Xmx2G -Xms512M"

                # Build command arguments (base_cmd is already the full path in container)
                cmd_args = [base_cmd, container_source_path, "-o", container_cpg_path]

                # Add Java opts as environment variables (Engine scripts read JAVA_OPTS)
                env = os.environ.copy()
                if java_opts:
                    env["JAVA_OPTS"] = java_opts
                    logger.info(f"Using JAVA_OPTS: {java_opts}")

                # Apply exclusions for languages that support them
                if (
                    language in self.config.cpg.languages_with_exclusions
                    and self.config.cpg.exclusion_patterns
                ):
                    # Escape special regex characters in patterns and combine with OR
                    escaped_patterns = [self._escape_regex_pattern(p) for p in self.config.cpg.exclusion_patterns]
                    combined_regex = "|".join(f"({p})" for p in escaped_patterns)
                    cmd_args.extend(["--exclude-regex", combined_regex])
                    logger.info(f"Applied {len(self.config.cpg.exclusion_patterns)} exclusion patterns")

                logger.info(f"Executing CPG generation: {' '.join(cmd_args)}")

                # Execute with timeout (run inside container)
                try:
                    with tracer.start_as_current_span("cpg.engine_cli_exec") as exec_span:
                        exec_span.set_attribute("cpg.command", base_cmd)
                        result = self._exec_command_sync(cmd_args, env, self.config.cpg.generation_timeout)

                    truncation_length = self.config.cpg.output_truncation_length
                    logger.info(f"CPG generation output:\n{result[:truncation_length]}")

                    # Check for fatal errors
                    if "ERROR:" in result or "Exception" in result:
                        truncation_length = self.config.cpg.output_truncation_length
                        logger.error(f"CPG generation reported fatal errors:\n{result[:truncation_length]}")
                        error_msg = "Engine reported fatal errors during CPG generation"
                        raise CPGGenerationError(error_msg)

                    # Validate CPG was created on disk using host path
                    if self._validate_cpg(cpg_path):
                        logger.info(f"CPG generation completed: {cpg_path}")

                        # Spawn analysis engine and load CPG if manager is available
                        engine_port = None
                        if self.engine_pool:
                            try:
                                with tracer.start_as_current_span("cpg.spawn_server") as srv_span:
                                    logger.info(f"Spawning analysis engine for codebase {codebase_hash}")
                                    engine_port = self.engine_pool.spawn_server(codebase_hash)
                                    srv_span.set_attribute("cpg.engine_port", engine_port)
                                    logger.info(f"analysis engine spawned successfully on port {engine_port}")

                                with tracer.start_as_current_span("cpg.load_cpg"):
                                    logger.info(f"Loading CPG into analysis engine on port {engine_port}")
                                    if self.engine_pool.load_cpg(codebase_hash, cpg_path):
                                        logger.info(f"CPG loaded into analysis engine successfully on port {engine_port}")
                                    else:
                                        logger.warning("Failed to load CPG into analysis engine")
                                        # Don't fail the whole operation, but log the issue
                            except Exception as e:
                                logger.error(f"Failed to setup analysis engine for {codebase_hash}: {e}", exc_info=True)
                                # Don't fail the whole operation, but the CPG is still usable
                        else:
                            logger.warning("engine_pool is None - cannot spawn analysis engine")

                        logger.info(f"Returning CPG path: {cpg_path}, engine_port: {engine_port}")
                        return cpg_path, engine_port
                    else:
                        error_msg = "CPG file was not created"
                        truncation_length = self.config.cpg.output_truncation_length
                        logger.error(f"{error_msg}: {result[:truncation_length]}")
                        raise CPGGenerationError(error_msg)

                except asyncio.TimeoutError:
                    error_msg = (
                        f"CPG generation timed out after {self.config.cpg.generation_timeout}s"
                    )
                    logger.error(error_msg)
                    raise CPGGenerationError(error_msg)

            except CPGGenerationError:
                raise
            except Exception as e:
                error_msg = f"CPG generation failed: {str(e)}"
                logger.error(error_msg)
                raise CPGGenerationError(error_msg)

    def _calculate_repo_size_mb(self, source_path: str) -> int:
        """Calculate total repository size in MB

        Args:
            source_path: Path to the repository directory

        Returns:
            Size in MB
        """
        try:
            total_size = 0
            for dirpath, dirnames, filenames in os.walk(source_path):
                # Skip .git directories and other common exclusions for size calculation
                dirnames[:] = [d for d in dirnames if d not in {'.git', '.svn', '.hg', '.idea', '.vscode', 'node_modules'}]

                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    try:
                        total_size += os.path.getsize(filepath)
                    except OSError as e:
                        logger.warning(f"Failed to get size of {filepath}: {e}")

            size_mb = total_size / (1024 * 1024)
            return int(size_mb)
        except Exception as e:
            logger.error(f"Failed to calculate repository size: {e}")
            raise CPGGenerationError(f"Failed to calculate repository size: {e}")

    def _escape_regex_pattern(self, pattern: str) -> str:
        """Escape special regex characters while preserving regex patterns

        Args:
            pattern: The pattern that may contain regex

        Returns:
            Escaped pattern safe for use in regex
        """
        # Don't escape regex metacharacters that are likely intentional
        # Just validate the pattern is valid regex
        try:
            re.compile(pattern)
            return pattern
        except re.error as e:
            logger.warning(f"Invalid regex pattern '{pattern}': {e}. Using literal match.")
            # If regex is invalid, escape it for literal matching
            return re.escape(pattern)

    def _host_to_container_path(self, host_path: str) -> str:
        """Convert host path to container path
        
    The container mounts ./workspace as /workspace
    So /home/aleks/workspace/zerograph/var/zerograph/ws/graphs/hash/graph.bin 
        becomes /var/zerograph/ws/graphs/hash/graph.bin
        """
        # Find the workspace directory in the path
        if "/var/zerograph/ws/" not in host_path:
            logger.warning(f"Path doesn't contain '/var/zerograph/ws/': {host_path}")
            return host_path
        
        # Extract everything after /var/zerograph/ws/
        parts = host_path.split("/var/zerograph/ws/")
        if len(parts) >= 2:
            return f"/var/zerograph/ws/{parts[-1]}"
        
        return host_path

    def _exec_command_sync(self, cmd_args: list, env: dict, timeout: int) -> str:
        """Execute command synchronously INSIDE Docker container with timeout"""
        # Get the container name from environment or use default
        container_name = os.getenv("ENGINE_CONTAINER_NAME", "zg-runtime")
        
        # Build docker exec command
        # Format: docker exec -e VAR=value CONTAINER COMMAND
        docker_cmd = ["docker", "exec"]
        
        # Add environment variables BEFORE the container name
        for key, value in env.items():
            if key not in os.environ or env[key] != os.environ[key]:
                docker_cmd.extend(["-e", f"{key}={value}"])
        
        # Container name
        docker_cmd.append(container_name)
        
        # The actual command to run inside container
        docker_cmd.extend(cmd_args)
        
        logger.info(f"Executing in container: {' '.join(docker_cmd)}")
        
        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            logger.info(f"Docker exec return code: {result.returncode}")
            
            # Combine stdout and stderr
            output = result.stdout + result.stderr
            if output:
                logger.debug(f"Command output: {output[:500]}")
            
            return output
        except subprocess.TimeoutExpired as e:
            logger.error(f"Docker exec command timed out after {timeout}s")
            raise asyncio.TimeoutError(f"Command timed out after {timeout}s") from e
        except Exception as e:
            logger.error(f"Error executing docker exec: {e}")
            raise

    def _validate_cpg(self, cpg_path: str) -> bool:
        """Validate that CPG file was created successfully and is not empty"""
        try:
            # Check if file exists
            if not os.path.exists(cpg_path):
                logger.error(f"CPG file not found: {cpg_path}")
                return False

            # Check file size
            file_size = os.path.getsize(cpg_path)
            min_cpg_size = self.config.cpg.min_cpg_file_size

            if file_size < min_cpg_size:
                logger.error(
                    f"CPG file is too small ({file_size} bytes), likely empty or corrupted. "
                    f"Minimum expected size: {min_cpg_size} bytes"
                )
                return False

            logger.info(
                f"CPG file created successfully: {cpg_path} (size: {file_size} bytes)"
            )
            return True

        except Exception as e:
            logger.error(f"CPG validation failed: {e}")
            return False

    def cleanup(self):
        """Cleanup (no-op in container)"""
        logger.info("CPG Generator cleanup (no-op)")

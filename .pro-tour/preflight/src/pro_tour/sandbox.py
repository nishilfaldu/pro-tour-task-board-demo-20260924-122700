"""Disposable app/browser namespaces with two socket volumes, one writer each.

This is a runtime isolation primitive, not the full PR pipeline. It accepts only
an already-built, trusted image ID; PR source ingestion and dependency egress
aren't implemented here. The controller exposes no endpoint to app/browser peers.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


class SandboxError(RuntimeError):
    """A sanitized operation failure; raw container output is not a diagnostic."""


class Docker:
    """Invoke the local daemon without ambient Docker credentials or plugins."""

    def __init__(self, socket_path: Path | None = None):
        self.executable = shutil.which("docker")
        if not self.executable:
            raise SandboxError("Docker CLI is unavailable")
        candidates = (Path("/var/run/docker.sock"), Path.home() / ".docker/run/docker.sock")
        self.socket = socket_path or next((path for path in candidates if path.is_socket()), None)
        if self.socket is None or not self.socket.is_socket():
            raise SandboxError("A local Docker daemon socket is required")

    def run(self, *args: str, timeout: float = 30, input_text: str | None = None) -> str:
        with tempfile.TemporaryDirectory(prefix="pro-tour-docker-") as temporary:
            environment = {
                "PATH": os.defpath,
                "HOME": temporary,
                "DOCKER_CONFIG": temporary,
                "LANG": "C",
            }
            try:
                result = subprocess.run(
                    [self.executable, "--host", f"unix://{self.socket}", *args],
                    env=environment,
                    cwd=temporary,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise SandboxError(
                    "Docker operation unavailable or exceeded its deadline"
                ) from None
        if result.returncode:
            raise SandboxError(
                "Docker operation failed; inspect the named sandbox with trusted tools"
            )
        return result.stdout


class SandboxSession:
    """Own exactly three disposable containers and two private named volumes.

    Use as a context manager so creation, startup, cancellation, and test failure
    all go through cleanup. No arbitrary mounts/network flags/environment can be
    supplied by a caller. The fixture controller also has network=none; provider
    client egress will require a separate, proven policy before integration.
    """

    # Code-owned policies. PR configuration never supplies these maps.
    CHANNELS = ("app", "control")
    MOUNTS = {
        "app": (("app", False),),
        "browser": (("app", True), ("control", False)),
        "controller": (("control", True),),
    }
    NETWORKS: dict[str, str] = {}
    MEMORY = {"browser": "768m"}

    def __init__(self, docker: Docker, image_id: str):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise SandboxError("Supply an immutable trusted local image ID")
        self.docker = docker
        self.image_id = image_id
        self.identifier = uuid.uuid4().hex
        self.prefix = f"pro-tour-{self.identifier}"
        self.containers: dict[str, str] = {}
        self.volumes: list[str] = []
        self.cleanup_errors: list[str] = []
        self._entered = False

    def __enter__(self):
        if self._entered:
            raise SandboxError("Sandbox session is already active")
        try:
            info = json.loads(self.docker.run("info", "--format", "{{json .}}"))
            if info.get("OSType") != "linux":
                raise SandboxError("A Linux Docker daemon is required")
            for kind in self.CHANNELS:
                name = f"{self.prefix}-{kind}"
                # Register names before mutation so even an interrupted command
                # or lost successful response is covered by cleanup.
                self.volumes.append(name)
                self.docker.run("volume", "create", "--label", self.label, name)
            self._entered = True
            return self
        except BaseException:
            self.close()
            raise

    @property
    def label(self) -> str:
        return f"pro-tour.session={self.identifier}"

    def start(self, role: str, argv: list[str]) -> str:
        if not self._entered:
            raise SandboxError("Open the sandbox session before starting processes")
        if role not in self.MOUNTS or role in self.containers:
            raise SandboxError("Use each supported sandbox role once per session")
        if (
            not argv
            or not argv[0]
            or any(not isinstance(value, str) or "\0" in value for value in argv)
        ):
            raise SandboxError("Supply a nonempty command argument array")
        name = f"{self.prefix}-{role}"
        self.containers[role] = name
        mounts = self.MOUNTS[role]
        memory = self.MEMORY.get(role, "128m")
        options = [
            "create",
            "--name",
            name,
            "--label",
            self.label,
            "--pull",
            "never",
            "--init",
            "--network",
            self.NETWORKS.get(role, "none"),
            "--user",
            "10000:10000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--read-only",
            "--pids-limit",
            "256",
            "--cpus",
            "1",
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--shm-size",
            "128m",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
            "--tmpfs",
            "/home/protour:rw,nosuid,nodev,size=64m,mode=700,uid=10000,gid=10000",
            "--env",
            "HOME=/home/protour",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            # Avoid trusting a caller-supplied image entrypoint to sanitize the
            # environment. Image selection itself is a controller responsibility.
            "--entrypoint",
            "/usr/bin/env",
        ]
        for kind, readonly in mounts:
            mount = f"type=volume,src={self.prefix}-{kind},dst=/run/pro-tour/{kind}"
            options.extend(["--mount", mount + (",readonly" if readonly else "")])
        self.docker.run(
            *options,
            self.image_id,
            "-i",
            "HOME=/home/protour",
            "LANG=C.UTF-8",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            *argv,
        )
        self.docker.run("start", name)
        return name

    def inspect(self, role: str) -> dict:
        return json.loads(self.docker.run("inspect", self.containers[role]))[0]

    def execute(self, role: str, argv: list[str], *, timeout: float = 30) -> str:
        """Execute a trusted diagnostic as the same unprivileged container user.

        This does not route PR instructions into the controller. Callers choose
        code-owned commands; app commands belong only to the app role.
        """
        return self.docker.run(
            "exec",
            self.containers[role],
            "/usr/bin/env",
            "-i",
            "HOME=/home/protour",
            "LANG=C.UTF-8",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            *argv,
            timeout=timeout,
        )

    def close(self) -> None:
        self.cleanup_errors.clear()
        self._entered = False
        for name in reversed(list(self.containers.values())):
            try:
                self.docker.run("rm", "--force", name)
            except SandboxError:
                self.cleanup_errors.append("container-removal")
        for name in reversed(self.volumes):
            try:
                self.docker.run("volume", "rm", name)
            except SandboxError:
                self.cleanup_errors.append("volume-removal")
        # A removal can report not-found after an interrupted create; the final
        # label queries are authoritative. Never delete resources we don't own.
        remaining = self.docker.run("ps", "-aq", "--filter", f"label={self.label}").strip()
        volumes = self.docker.run("volume", "ls", "-q", "--filter", f"label={self.label}").strip()
        if remaining or volumes:
            raise SandboxError("Sandbox cleanup incomplete; owned resources remain")
        self.containers.clear()
        self.volumes.clear()
        self.cleanup_errors.clear()

    def __exit__(self, *_):
        self.close()

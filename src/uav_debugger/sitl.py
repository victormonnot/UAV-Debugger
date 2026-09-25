"""Launch one fingerprinted ArduCopter SITL profile in a loopback-only namespace."""

from __future__ import annotations

import fcntl
import hashlib
import os
import platform
import signal
import socket
import stat
import struct
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

BINARY_VERSION = "ArduCopter V4.6.3"
BINARY_SHA256 = "7862662092edc2861fc03da3d6fb2f0136d1670e563ca324eb52c1a324d1e14b"
BINARY_URL = "https://firmware.ardupilot.org/Copter/stable-4.6.3/SITL_x86_64_linux_gnu/arducopter"
GIT_COMMIT = "3fc7011a7d3dc047cbb17d8bd98ee94577d144c6"
PROFILE = "arducopter-4.6.3-linux-x86_64-v1"
# GCS_MAVLINK::init sends this SiK bootloader sequence exactly three times,
# before MAVLink traffic. It is raw startup data, never a MAVLink frame:
# https://github.com/ArduPilot/ardupilot/blob/3fc7011a7d3dc047cbb17d8bd98ee94577d144c6/libraries/GCS_MAVLink/GCS_Common.cpp#L164
STARTUP_PREAMBLE = b"0 "
STARTUP_PREAMBLE_COUNT = 3

# This telemetry-only profile leaves the vehicle disarmed. SR0 names the first
# active MAVLink channel, here SERIAL1; SERIAL0 also emits raw console text.
DEFAULT_PARAMETERS = """FRAME_CLASS 1
FRAME_TYPE 0
SERIAL0_PROTOCOL -1
SERIAL1_PROTOCOL 2
SERIAL2_PROTOCOL -1
SR0_EXTRA1 20
SR0_EXT_STAT 0
SR0_EXTRA2 0
SR0_EXTRA3 0
SR0_POSITION 0
SR0_RAW_CTRL 0
SR0_RAW_SENS 0
SR0_RC_CHAN 0
SR0_PARAMS 0
LOG_BACKEND_TYPE 0
"""


def validate_binary(path: Path) -> dict:
    """Fingerprint an already installed binary without executing or downloading it."""
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError("This SITL profile requires Linux x86_64")
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("SITL binary must be a regular file")
    with resolved.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("SITL binary must be a regular file")
        # Bound reads even for the wrong file or one growing during verification.
        content = stream.read(16 * 1024 * 1024 + 1)
    digest = hashlib.sha256(content).hexdigest()
    if len(content) > 16 * 1024 * 1024 or digest != BINARY_SHA256:
        raise ValueError(f"SITL binary SHA-256 does not match the supported {BINARY_VERSION} build")
    if not os.access(resolved, os.X_OK):
        raise ValueError("SITL binary is not executable")
    return {
        "path": str(resolved),
        "sha256": digest,
        "size_bytes": len(content),
        "version": BINARY_VERSION,
        "source_url": BINARY_URL,
        "git_commit": GIT_COMMIT,
        "profile": PROFILE,
    }


def require_loopback_only() -> None:
    """Reject any namespace with a non-loopback interface, even if that interface is down."""
    message = "SITL requires a network namespace with only loopback enabled"
    if sys.platform != "linux" or {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError(message)
    # The pinned AP_RCProtocol_UDP backend binds 0.0.0.0 unconditionally, even
    # with RC_PROTOCOLS=0. --rc-in-port 0 selects an ephemeral port; the namespace
    # is what prevents external access. See the pinned upstream implementation:
    # https://github.com/ArduPilot/ardupilot/blob/3fc7011a7d3dc047cbb17d8bd98ee94577d144c6/libraries/AP_RCProtocol/AP_RCProtocol_UDP.cpp
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        request = struct.pack("16sH14s", b"lo", 0, b"")
        flags = struct.unpack("16sH14s", fcntl.ioctl(probe.fileno(), 0x8913, request))[1]
    if flags & 0x9 != 0x9:  # Linux IFF_UP | IFF_LOOPBACK.
        raise RuntimeError(message)


class SITLProcess:
    """Own one direct subprocess and its new process group; never target another run."""

    def __init__(self, binary: Path, output: Path, relay_port: int):
        if (
            isinstance(relay_port, bool)
            or not isinstance(relay_port, int)
            or not 1 <= relay_port <= 65535
        ):
            raise ValueError("Relay port must be an integer between 1 and 65535")
        identity = validate_binary(binary)
        self.output = Path(output).resolve()
        self.workdir = self.output / "simulator"
        self.defaults_path = self.workdir / "profile.parm"
        self.log_path = self.output / "simulator.log"
        self.process: subprocess.Popen | None = None
        self._log: BinaryIO | None = None
        self._started = False
        self._stopped: dict | None = None
        arguments = [
            identity["path"],
            "--model",
            "quad",
            "--speedup",
            "1",
            "--wipe",
            "--home",
            "-35.363261,149.165230,584,353",
            "--serial1",
            f"udpclient:127.0.0.1:{relay_port}",
            "--defaults",
            str(self.defaults_path),
            "--rc-in-port",
            "0",
            "--sim-address",
            "127.0.0.1",
            "--sim-port-in",
            "0",
            "--sim-port-out",
            "0",
            "--irlock-port",
            "0",
        ]
        for port in (0, 2, 5, 6, 7, 8):
            arguments.extend((f"--serial{port}", "none"))
        self.metadata = {
            "binary": identity,
            "argv": arguments,
            "cwd": str(self.workdir),
            "defaults_path": str(self.defaults_path),
            "defaults": DEFAULT_PARAMETERS,
            "defaults_sha256": hashlib.sha256(DEFAULT_PARAMETERS.encode("ascii")).hexdigest(),
            "log_path": str(self.log_path),
            "network": "This network namespace must contain only the enabled loopback interface.",
            "auxiliary_socket": "The simulator RC UDP socket binds 0.0.0.0 on an ephemeral port.",
            "startup_preamble": {
                "hex": STARTUP_PREAMBLE.hex(),
                "maximum_datagrams": STARTUP_PREAMBLE_COUNT,
                "meaning": "SiK bootloader startup workaround, before the first MAVLink frame.",
            },
        }

    def start(self) -> subprocess.Popen:
        """Prepare fresh simulator state and launch without a shell or stdin commands."""
        if self._started:
            raise RuntimeError("SITL process has already been started")
        require_loopback_only()
        # Recheck immediately before launching, rather than trusting a prior CLI check.
        if validate_binary(Path(self.metadata["binary"]["path"])) != self.metadata["binary"]:
            raise ValueError("SITL binary changed after verification")
        self._started = True
        self.workdir.mkdir(exist_ok=False)
        with self.defaults_path.open("x", encoding="ascii") as defaults:
            defaults.write(DEFAULT_PARAMETERS)
        self._log = self.log_path.open("xb")
        try:
            self.process = subprocess.Popen(
                self.metadata["argv"],
                cwd=self.workdir,
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            self._log.close()
            self._log = None
            raise
        self.metadata["pid"] = self.process.pid
        return self.process

    def stop(self) -> dict:
        """Terminate the owned process group, escalate after two seconds and reap the child."""
        if self._stopped is not None:
            return self._stopped
        escalated = False
        try:
            if self.process is not None and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    escalated = True
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=1)
            self._stopped = {
                "returncode": self.process.returncode if self.process is not None else None,
                "escalated_to_kill": escalated,
            }
            self.metadata["shutdown"] = self._stopped
            return self._stopped
        finally:
            if self._log is not None:
                self._log.close()
                self._log = None

"""Owned worker for the optional local Experiment interface.

The parent owns stdin. Closing that pipe, including parent process death, requests
the runner's ordinary bounded cleanup. This module is never used by Analyze.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from .experiment import ExperimentConfig, run_experiment


def _watch_owner(stop: threading.Event) -> None:
    try:
        # A stop command or EOF both mean ownership ended. No command can start
        # another run or alter its requested configuration.
        os.read(sys.stdin.fileno(), 1)
    finally:
        stop.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Owned local Experiment worker")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=("baseline", "blackout"), required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--blackout-at", type=float)
    parser.add_argument("--sitl-binary", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=15.0)
    parser.add_argument("--isolated", action="store_true")
    args = parser.parse_args(argv)
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    # A namespace's PID 1 must install handlers explicitly, before any child.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_stop)
    threading.Thread(target=_watch_owner, args=(stop,), daemon=True).start()
    config = ExperimentConfig(
        args.scenario, args.duration, args.blackout_at, args.sitl_binary, args.startup_timeout
    )
    try:
        config.validate()
        if bool(args.sitl_binary) != args.isolated:
            raise ValueError("SITL workers require the isolated launch path")
        if args.isolated:
            if os.getpid() != 1 or {name for _, name in socket.if_nameindex()} != {"lo"}:
                raise ValueError("SITL worker must be PID 1 in its isolated network namespace")
            subprocess.run(
                ["ip", "link", "set", "dev", "lo", "up"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=3,
                check=True,
            )
            from .sitl import require_loopback_only

            require_loopback_only()
        manifest = run_experiment(args.output, config, stop=stop)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Cannot run experiment: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    if manifest["outcome"] == "failed":
        print(f"Experiment failed: {manifest['error']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

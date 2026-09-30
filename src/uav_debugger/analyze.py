"""Launch the local Analyze interface with an explicit loopback listener."""

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open UAV Debugger on this computer.")
    parser.add_argument("--port", type=int, default=8501, help="Local UI port (default: 8501)")
    parser.add_argument(
        "--experiment-root",
        type=Path,
        default=Path("local/experiments"),
        help="Local saved-run catalog and new browser outputs (created only on Start)",
    )
    parser.add_argument(
        "--sitl-binary",
        type=Path,
        help="Enable the pinned local SITL source for explicit browser Experiment launches",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.environ["UAV_DEBUGGER_EXPERIMENT_ROOT"] = str(args.experiment_root.absolute())
    if args.sitl_binary is not None:
        os.environ["UAV_DEBUGGER_UI_SITL_BINARY"] = str(args.sitl_binary.absolute())
    else:
        os.environ.pop("UAV_DEBUGGER_UI_SITL_BINARY", None)

    from streamlit.web import cli

    sys.argv = [
        "streamlit",
        "run",
        str(Path(__file__).with_name("app.py")),
        "--server.address=127.0.0.1",
        f"--server.port={args.port}",
        "--server.headless=true",
        "--server.fileWatcherType=none",
        # Streamlit's uploader uses decimal MB; the importer enforces 10 MiB.
        "--server.maxUploadSize=11",
        "--browser.serverAddress=127.0.0.1",
        "--browser.gatherUsageStats=false",
        "--client.toolbarMode=minimal",
        "--theme.base=light",
        "--theme.primaryColor=#007f6d",
        "--theme.backgroundColor=#ffffff",
        "--theme.secondaryBackgroundColor=#f2f5f7",
        "--theme.textColor=#18313c",
        "--theme.font=sans-serif",
    ]
    try:
        return cli.main()
    finally:
        # File-only Analyze never imports the execution controller.
        control = sys.modules.get("uav_debugger.experiment_control")
        if control is not None:
            control.shutdown_all()


if __name__ == "__main__":
    raise SystemExit(main())

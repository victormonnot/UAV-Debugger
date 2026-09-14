"""Launch the local Analyze interface with an explicit loopback listener."""

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open UAV Debugger Analyze on this computer.")
    parser.add_argument("--port", type=int, default=8501, help="Local UI port (default: 8501)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

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
    return cli.main()


if __name__ == "__main__":
    raise SystemExit(main())

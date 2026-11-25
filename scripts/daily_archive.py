"""Run the daily collector against the existing local archive."""

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check CLI startup only")
    args = parser.parse_args()
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    kind = "check" if args.check else "daily"
    commands = [("discover", "--help")] if args.check else [
        ("discover", "--provider", "selenium", "--workers", "4"),
        ("download", "--mode", "hybrid", "--workers", "4", "--limit", "100"),
    ]
    result = 0
    with (logs / f"archive-{kind}-{stamp}.log").open("w", encoding="utf-8") as log:
        for command in commands:
            print(f"{datetime.now().isoformat(timespec='seconds')} {' '.join(command)}", file=log, flush=True)
            status = subprocess.call(
                [sys.executable, "-X", "utf8", "-u", "-m", "petrovaradin_archive.cli", *command],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            print(f"exit_code={status}", file=log, flush=True)
            if status and not result:
                result = status
    return result


if __name__ == "__main__":
    raise SystemExit(main())

"""Apply to jobs with the PDF that matches each selected role.

This is the same command as ``rolefit apply``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    """Run the apply command with this process's arguments."""
    from typing import Any, cast

    from typer.main import get_command

    from rolefit.cli import app

    cli = cast(Any, get_command(app))
    cli.commands["apply"].main(args=sys.argv[1:], prog_name="apply_jobs.py")


if __name__ == "__main__":
    main()

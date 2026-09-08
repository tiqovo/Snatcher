import os
import sys

from snatcher.cli import main


if os.name == "nt":
    os.system("chcp 65001 > nul")
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.demo import create_demo_repository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", default="examples/generated")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    print(create_demo_repository(args.target, replace=args.replace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


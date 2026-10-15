#!/usr/bin/env python3
"""Inspect the installed mosherdr release without reading terminal contents."""

import argparse
import json
import sys

sys.dont_write_bytecode = True
VERSION = "0.1.0"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version="mosherdr " + VERSION)
    parser.add_argument("command", choices=("status",))
    args = parser.parse_args(argv)
    if args.command == "status":
        from install import installation_status
        result = installation_status()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())

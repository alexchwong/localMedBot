#!/usr/bin/env python3
"""Repository maintenance checks."""
from __future__ import annotations

import argparse

from _maintenance_common import ToolError, check_maintenance, checkout_root, json_failure, json_success


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError("argument_invalid")


def main(argv=None) -> int:
    parser = QuietParser(prog="maintenance.py")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    try:
        args = parser.parse_args(argv)
        root = checkout_root(__file__)
        if args.command == "check":
            metadata = check_maintenance(root)
            return json_success({"status": "ok", "version": metadata["version"]})
        raise ToolError("argument_invalid")
    except ToolError as exc:
        return json_failure(exc)
    except Exception:
        return json_failure(ToolError("internal_error"))


if __name__ == "__main__":
    raise SystemExit(main())

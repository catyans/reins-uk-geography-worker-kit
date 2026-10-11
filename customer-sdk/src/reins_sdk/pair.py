"""Create a local worker code for the isolated Reins demonstration."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

from .client import ReinsClient, ReinsError

DEFAULT_DEMO_API_URL = "https://47.245.114.167:8444/api"
DEFAULT_TOKEN_FILE = Path.home() / ".config/reins/demo-worker.token"


def create_code(path: Path) -> str:
    """Create or reuse a private code; never replace a paired code implicitly."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Pairing code file must be a regular private file (0600)")
        code = path.read_text().strip()
        if len(code) < 43:
            raise ValueError("Existing pairing code is invalid")
        return code
    code = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(code + "\n")
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", nargs="?", help="Optional specific task ID to check")
    parser.add_argument("--output", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--base-url", default=DEFAULT_DEMO_API_URL)
    parser.add_argument(
        "--check", action="store_true", help="Check whether the server paired this code"
    )
    args = parser.parse_args()
    try:
        code = create_code(args.output)
        if args.check:
            client = ReinsClient(args.base_url, token=code)
            if args.task_id:
                result = client.status(args.task_id)
                if len(result.get("workflows", [])) != 1:
                    raise ValueError("Pairing did not return the requested task")
                print("Connected to Reins task:", args.task_id)
            else:
                result = client.pairing()
                print("Connected to Reins customer:", result["customer_id"])
            return
        if args.task_id:
            print("Task ID:", args.task_id)
        else:
            print("The Reins operator will pair this code using a Hosted task.")
        print("Pairing code (paste into Reins Settings > Pair a customer SDK):")
        print(code)
        print("Saved privately at:", args.output.expanduser())
        print("After approval, run: python -m reins_sdk.pair --check")
        print("Each new run receives an internal task ID automatically; no new pairing is needed.")
    except (OSError, ValueError, ReinsError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()

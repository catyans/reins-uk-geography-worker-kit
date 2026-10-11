#!/usr/bin/env python3
"""Copy only the public Reins integration skill into a customer repository."""

from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "skills" / "reins-integrate"
DESTINATIONS = {
    "codex": Path(".agents/skills/reins-integrate"),
    "claude": Path(".claude/skills/reins-integrate"),
}


def same_tree(source: Path, target: Path) -> bool:
    if not target.is_dir():
        return False
    source_files = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
    target_files = {p.relative_to(target) for p in target.rglob("*") if p.is_file()}
    return source_files == target_files and all(
        filecmp.cmp(source / item, target / item, shallow=False) for item in source_files
    )


def install(target: Path, assistant: str, *, dry_run=False, force=False, uninstall=False):
    if not target.is_dir():
        raise ValueError("Target must be an existing repository directory")
    target = target.resolve()
    choices = list(DESTINATIONS) if assistant == "both" else [assistant]
    paths = [target / DESTINATIONS[name] for name in choices]
    for path in paths:
        current = target
        for component in path.relative_to(target).parts:
            current = current / component
            if current.is_symlink():
                raise ValueError(f"Refusing a symlinked skill path: {current}")
        if path.exists() and not same_tree(SOURCE, path) and not force:
            raise ValueError(f"Refusing to replace a modified skill: {path} (use --force)")
    for path in paths:
        if uninstall:
            if path.exists():
                if not dry_run:
                    shutil.rmtree(path)
                print(("Would remove " if dry_run else "Removed ") + str(path))
        elif same_tree(SOURCE, path):
            print("Already installed: " + str(path))
        elif dry_run:
            print("Would install " + str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                shutil.rmtree(path)
            shutil.copytree(SOURCE, path)
            print("Installed " + str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True, help="Customer repository directory")
    parser.add_argument("--assistant", choices=["codex", "claude", "both"], default="codex")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Replace a modified skill copy")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    try:
        install(
            args.target,
            args.assistant,
            dry_run=args.dry_run,
            force=args.force,
            uninstall=args.uninstall,
        )
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

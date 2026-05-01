"""Interactively review the pending pantry-candidate list.

For each ingredient remaining in ``pending.txt``, prompts ``y/n/e`` (q to quit):
  - ``y`` -> append to ``approved.txt`` AND drop from ``pending.txt``
  - ``n`` -> drop from ``pending.txt`` (no approval)
  - ``e`` -> prompt for an edited value, append edited value to ``approved.txt``
            AND drop the original from ``pending.txt``
  - ``q`` -> save current state and exit

The pending file is rewritten after every answer, so an interrupted session
resumes cleanly the next time the script is run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PENDING = PROJECT_ROOT / "data" / "pantry_candidates" / "pending.txt"
DEFAULT_APPROVED = PROJECT_ROOT / "data" / "pantry_candidates" / "approved.txt"


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")


def prompt(item: str) -> str:
    while True:
        ans = input(f"Add to pantry? [y/n/e/q] {item!r}: ").strip().lower()
        if ans in {"y", "n", "e", "q"}:
            return ans
        print("  please answer y, n, e, or q")


def prompt_edit(item: str) -> str | None:
    """Ask for an edited value. Returns the new value, or None to cancel."""
    while True:
        new = input(f"  edited value (blank to cancel) [{item}]: ").strip()
        if not new:
            return None
        return new


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pending", type=Path, default=DEFAULT_PENDING)
    parser.add_argument("--approved", type=Path, default=DEFAULT_APPROVED)
    args = parser.parse_args()

    pending = read_lines(args.pending)
    approved = read_lines(args.approved)
    approved_set = set(approved)

    if not pending:
        print(f"No pending items in {args.pending}.")
        return

    print(f"{len(pending)} item(s) to review. {len(approved)} already approved.\n")

    yes_count = no_count = edit_count = 0
    try:
        while pending:
            item = pending[0]
            try:
                ans = prompt(item)
            except EOFError:
                ans = "q"

            if ans == "q":
                print("\nQuitting. Progress saved.")
                break

            if ans == "e":
                try:
                    edited = prompt_edit(item)
                except EOFError:
                    edited = None
                if edited is None:
                    print("  edit cancelled")
                    continue
                pending.pop(0)
                if edited not in approved_set:
                    approved.append(edited)
                    approved_set.add(edited)
                edit_count += 1
            else:
                pending.pop(0)
                if ans == "y":
                    if item not in approved_set:
                        approved.append(item)
                        approved_set.add(item)
                    yes_count += 1
                else:
                    no_count += 1

            write_lines(args.pending, pending)
            write_lines(args.approved, approved)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress saved.")

    print(
        f"\nSession summary: +{yes_count} approved, ~{edit_count} edited, "
        f"-{no_count} skipped. {len(pending)} pending, {len(approved)} approved total."
    )


if __name__ == "__main__":
    main()

"""Extract one release's notes from CHANGELOG.md, and prove the version agrees.

Used by ``.github/workflows/release.yml``. Three files state the version of a
release — the git tag, ``backend/pyproject.toml`` and the CHANGELOG heading —
and nothing until now compared them. A tag pushed with any one of the three out
of step would publish a release whose notes describe a different version, so
this refuses to emit anything unless all three agree.

Deliberately dependency-free: it runs on a bare runner before any toolchain is
installed. ``tomllib`` is standard library from 3.11.

Usage:
    python .github/scripts/release_notes.py --tag v0.1.0 --output notes.md
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

# ``## [0.1.0] — 2026-08-26``. The separator is an em dash in this changelog,
# but the date is not what we key on, so anything after the bracket is ignored.
HEADING = re.compile(r"^##\s+\[(?P<version>[^\]]+)\]")

# The link-reference block at the foot of the file
# (``[0.1.0]: https://github.com/…``). It follows the last section with no
# heading of its own, so without this the final release's notes would swallow
# it.
LINK_DEF = re.compile(r"^\[[^\]]+\]:\s+\S")


def version_from_tag(tag: str) -> str:
    """``v0.1.0`` -> ``0.1.0``. The leading ``v`` is required by convention."""
    if not tag.startswith("v"):
        sys.exit(f"error: tag {tag!r} does not start with 'v'")
    return tag[1:]


def version_from_pyproject(path: Path) -> str:
    with path.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def section(changelog: str, version: str) -> str:
    """Return the body of the ``## [version]`` section, heading excluded.

    Raises ``LookupError`` if there is no such heading. An empty body is
    returned as-is; the caller decides that it is fatal.
    """
    lines = changelog.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match is None:
            continue
        if match.group("version") == version:
            start = index + 1
            continue
        if start is not None:
            # The next release's heading ends this one.
            return "\n".join(lines[start:index]).strip()
    if start is None:
        raise LookupError(version)
    for index in range(start, len(lines)):
        if LINK_DEF.match(lines[index]):
            return "\n".join(lines[start:index]).strip()
    return "\n".join(lines[start:]).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="annotated tag, e.g. v0.1.0")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--pyproject", type=Path, default=Path("backend/pyproject.toml"))
    parser.add_argument("--output", type=Path, help="write notes here (default: stdout)")
    args = parser.parse_args()

    version = version_from_tag(args.tag)

    packaged = version_from_pyproject(args.pyproject)
    if packaged != version:
        sys.exit(
            f"error: tag {args.tag} says {version}, but {args.pyproject} "
            f"says {packaged}. Bump one of them; do not publish the mismatch."
        )

    try:
        notes = section(args.changelog.read_text(encoding="utf-8"), version)
    except LookupError:
        sys.exit(
            f"error: {args.changelog} has no '## [{version}]' section. "
            f"Every release documents itself before it is tagged."
        )

    if not notes:
        sys.exit(f"error: the '## [{version}]' section of {args.changelog} is empty.")

    if args.output:
        args.output.write_text(notes + "\n", encoding="utf-8")
        print(f"wrote {len(notes.splitlines())} lines of notes for {version} to {args.output}")
    else:
        print(notes)


if __name__ == "__main__":
    main()

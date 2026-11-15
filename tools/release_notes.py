"""Release helper used by .github/workflows/release.yml.

    python tools/release_notes.py v1.2 release-notes.md
    python tools/release_notes.py v1.2 release-notes.md --checksum dist/SHA256.txt

Checks that the tag matches DISPLAY_VERSION in optimizer.py (so a release can't ship with the
wrong version inside), then writes that version's CHANGELOG.md section as the release notes.
Exits with an error message when either check fails.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def app_version() -> str:
    source = (ROOT / "optimizer.py").read_text(encoding="utf-8")
    match = re.search(r'^DISPLAY_VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if not match:
        raise SystemExit("DISPLAY_VERSION not found in optimizer.py")
    return match.group(1)


def changelog_section(tag: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    pattern = rf"^## {re.escape(tag)}(?:[ \t]+-[ \t]+[^\n]*)?\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    if not match or not match.group(1).strip():
        raise SystemExit(f"CHANGELOG.md has no section for {tag}. Add a '## {tag} - YYYY-MM-DD' heading first.")
    return match.group(1).strip()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    tag, out = argv[1], pathlib.Path(argv[2])
    checksum = None
    if "--checksum" in argv:
        checksum = pathlib.Path(argv[argv.index("--checksum") + 1]).read_text(encoding="utf-8").strip()
    version = app_version()
    if tag != version:
        raise SystemExit(f"Tag {tag} doesn't match DISPLAY_VERSION {version} in optimizer.py. "
                         "Update the version in optimizer.py, commit, and tag again.")
    notes = [changelog_section(tag), "",
             "### Download",
             "Download **Astra.Optimizer.exe** below and run it. It asks for administrator rights once (UAC).",
             "The exe isn't code-signed, so Windows may show *Windows protected your PC*: choose **More info**, "
             "then **Run anyway**.",
             "",
             "Built by GitHub Actions from this tag's source code."]
    if checksum:
        notes += ["", f"SHA-256: `{checksum}`"]
    out.write_text("\n".join(notes) + "\n", encoding="utf-8")
    print(f"Release notes for {tag} written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

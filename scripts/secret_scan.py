"""Fail on a small set of high-confidence credential patterns.

This intentionally complements, rather than replaces, GitHub secret scanning. It
prints only the file and line number so a CI log does not repeat a discovered secret.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{70,})\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "credential-url": re.compile(r"https?://[^\s/:]+:[^\s/@]+@[^\s/]+"),
}


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for path in candidate_files():
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append((str(path.relative_to(ROOT)), line_number, name))

    for path, line_number, name in findings:
        print(f"{path}:{line_number}: possible {name}")
    if findings:
        print(f"Secret scan failed with {len(findings)} high-confidence finding(s).")
        return 1
    print(f"Secret scan passed across {len(candidate_files())} repository file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

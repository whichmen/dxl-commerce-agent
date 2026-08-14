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
    "openai-style-token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "bearer-token": re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{20,}\b", re.IGNORECASE),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "absolute-user-home": re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/"),
    "private-ipv4": re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
        r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|"
        r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2})\b"
    ),
}

DENIED_FILENAMES = {
    "auth-profiles.json",
    "openclaw.json",
    "local.properties",
    "taobao_accounts.conf",
}

DENIED_PARTS = {
    "chrome_user_data",
    "playwright/.auth",
    "session_state",
    "storage_state",
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
        relative = path.relative_to(ROOT)
        relative_text = relative.as_posix()
        if path.name in DENIED_FILENAMES or any(part in relative_text for part in DENIED_PARTS):
            findings.append((relative_text, 0, "sensitive-file"))
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append((relative_text, line_number, name))

    for path, line_number, name in findings:
        print(f"{path}:{line_number}: possible {name}")
    if findings:
        print(f"Secret scan failed with {len(findings)} high-confidence finding(s).")
        return 1
    print(f"Secret scan passed across {len(candidate_files())} repository file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

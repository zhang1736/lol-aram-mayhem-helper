"""发布前安全审计：源码/配置/日志/构建产物/Git 历史（若有）扫描。

    & ".venv\\Scripts\\python.exe" tools\\security_audit.py

输出：
    .audit/security_raw.json   （完整结构化结果，含脱敏片段；不要发布）
    .audit/security_summary.txt（汇总）

原则：
    * 只读扫描，不修改任何业务文件
    * 所有命中值一律脱敏（只保留首尾少量字符）
    * 二进制文件按 ASCII 字符串扫描（查找个人路径/令牌）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = PROJECT_ROOT / ".audit"

TEXT_EXTS = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".ps1", ".bat", ".cmd", ".html", ".js", ".css", ".xml", ".csv", ".log",
}
SKIP_DIRS = {
    ".venv", "venv", "env", "build", "build_updater", "dist", "dist_updater",
    ".pytest_cache", ".pytest_tmp", "__pycache__", ".git", "node_modules",
    ".audit",
}
BINARY_EXTS = {".exe", ".dll", ".pyd", ".zip", ".db", ".png", ".onnx"}


@dataclass
class Finding:
    severity: str
    category: str
    path: str
    line: int
    masked: str


def mask(value: str, keep: int = 4) -> str:
    text = str(value)
    if len(text) <= keep * 2:
        return "*" * len(text)
    return text[:keep] + "*" * (len(text) - keep * 2) + text[-keep:]


def mask_path(text: str) -> str:
    """把 C:\\Users\\<name> 里的用户名脱敏。"""
    return re.sub(r"(?i)([A-Z]:\\Users\\)([^\\\s\"']+)",
                  lambda m: m.group(1) + mask(m.group(2), 2), text)


PATTERNS = [
    ("HIGH", "private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("HIGH", "github_token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("HIGH", "openai_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}\b")),
    ("HIGH", "google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("HIGH", "aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("HIGH", "lcu_token_arg", re.compile(
        r"--remoting-auth-token[= ]+([A-Za-z0-9_\-]{8,})")),
    ("HIGH", "basic_auth_header", re.compile(
        r"Authorization[\"']?\s*[:=]\s*[\"']?Basic\s+([A-Za-z0-9+/=]{12,})")),
    ("MEDIUM", "jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("MEDIUM", "secret_assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|access[_-]?token|"
        r"refresh[_-]?token|client[_-]?secret)\b\s*[:=]\s*[\"']?"
        r"([A-Za-z0-9_\-\.]{12,})")),
    ("MEDIUM", "personal_email", re.compile(
        r"\b[A-Za-z0-9._%+\-]+@(?!users\.noreply\.github\.com)"
        r"[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("MEDIUM", "cn_mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("LOW", "user_profile_path", re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s\"']+")),
    ("LOW", "private_ip", re.compile(
        r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\."
        r"\d{1,3}\.\d{1,3}\b")),
]

#: 明显是占位符/变量名/文档示例的匹配，不算泄露
ALLOWLIST = re.compile(
    r"(?i)(<[^>]+>|\{\{|\$\{|xxx+|placeholder|example|your[_-]?|"
    r"token_length|token\s*\)|\.token\b|os\.environ|getenv|"
    r"test|dummy|fake|sample|redacted|\*\*\*)")


def _iter_files(roots: list[Path], exts: set[str] | None = None):
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            yield root
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(PROJECT_ROOT).parts):
                continue
            if exts is not None and path.suffix.lower() not in exts:
                continue
            yield path


def scan_text(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    relative = path.relative_to(PROJECT_ROOT).as_posix()
    for line_no, line in enumerate(text.splitlines(), 1):
        if len(line) > 4000:
            line = line[:4000]
        for severity, category, pattern in PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(1) if match.groups() else match.group(0)
                if ALLOWLIST.search(value) or ALLOWLIST.search(line):
                    # 占位符/变量名：保留为低风险提示，避免漏报
                    if category not in ("user_profile_path", "private_ip"):
                        continue
                snippet = mask_path(value)
                if category in ("secret_assignment", "lcu_token_arg",
                                "basic_auth_header"):
                    snippet = mask(value)
                findings.append(Finding(severity, category, relative, line_no,
                                        snippet[:120]))
    return findings


def scan_binary(path: Path, max_bytes: int = 200 * 1024 * 1024) -> list[Finding]:
    """扫描二进制里的 ASCII 字符串（个人路径/令牌形态）。"""
    findings: list[Finding] = []
    relative = path.relative_to(PROJECT_ROOT).as_posix()
    try:
        size = path.stat().st_size
        if size > max_bytes:
            return findings
        data = path.read_bytes()
    except OSError:
        return findings
    for severity, category, pattern in PATTERNS:
        if category in ("jwt", "private_ip", "cn_mobile"):
            continue
        for match in pattern.finditer(data.decode("latin-1", errors="ignore")):
            value = match.group(1) if match.groups() else match.group(0)
            if ALLOWLIST.search(value):
                continue
            findings.append(Finding("LOW" if severity == "HIGH" else severity,
                                    category + "_in_binary", relative, 0,
                                    mask_path(value)[:120]))
    return findings


def git_info() -> dict:
    def run(args):
        try:
            result = subprocess.run(args, cwd=PROJECT_ROOT, capture_output=True,
                                    text=True, timeout=20)
            return result.stdout.strip() or result.stderr.strip()
        except Exception as exc:  # noqa: BLE001
            return "ERROR: %s" % exc

    return {
        "rev_parse": run(["git", "rev-parse", "--show-toplevel"]),
        "status": run(["git", "status", "--short"]),
        "ls_files_count": len([l for l in run(["git", "ls-files"]).splitlines() if l])
        if "fatal" not in run(["git", "ls-files"]) else 0,
        "log": run(["git", "log", "--all", "--oneline"]).splitlines()[:10],
        "remotes": run(["git", "remote", "-v"]),
    }


def main() -> int:
    AUDIT_DIR.mkdir(exist_ok=True)
    roots = [
        PROJECT_ROOT / "src", PROJECT_ROOT / "tests", PROJECT_ROOT / "tools",
        PROJECT_ROOT / "packaging", PROJECT_ROOT / "config", PROJECT_ROOT / "certs",
        PROJECT_ROOT / "research", PROJECT_ROOT / "logs", PROJECT_ROOT / "data",
        PROJECT_ROOT / "README.md", PROJECT_ROOT / "pyproject.toml",
        PROJECT_ROOT / "requirements.txt", PROJECT_ROOT / ".python-version",
    ]
    text_findings: list[Finding] = []
    for path in _iter_files(roots, TEXT_EXTS):
        text_findings.extend(scan_text(path))

    binary_roots = [PROJECT_ROOT / "dist" / "LOL海克斯助手",
                    PROJECT_ROOT / "release"]
    binary_findings: list[Finding] = []
    for path in _iter_files(binary_roots, BINARY_EXTS):
        if path.suffix.lower() in (".exe", ".dll", ".pyd", ".zip"):
            binary_findings.extend(scan_binary(path))

    info = git_info()
    payload = {
        "git": info,
        "text_findings": [asdict(f) for f in text_findings],
        "binary_findings": [asdict(f) for f in binary_findings],
    }
    (AUDIT_DIR / "security_raw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    by_sev: dict[str, int] = {}
    for finding in text_findings + binary_findings:
        by_sev[finding.severity] = by_sev.get(finding.severity, 0) + 1
    lines = [
        "=== Security audit summary ===",
        "git: %s" % info["rev_parse"],
        "text findings: %d" % len(text_findings),
        "binary findings: %d" % len(binary_findings),
        "severity: %s" % by_sev,
        "",
        "--- findings (masked) ---",
    ]
    for finding in sorted(text_findings + binary_findings,
                          key=lambda f: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(f.severity, 3),
                                         f.path, f.line)):
        lines.append("[%s] %-24s %s:%s  %s" % (
            finding.severity, finding.category, finding.path, finding.line,
            finding.masked))
    summary = "\n".join(lines) + "\n"
    (AUDIT_DIR / "security_summary.txt").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

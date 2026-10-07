"""Identifier and secret leak scan (``finplan-conformance leak-scan``; OWN-03, ENV-08, CS-09).

Public repositories must contain no AWS account IDs, ARNs naming an account, real
bucket names, deployed endpoint URLs or secret values. Cross-repository resources
are referenced only through SSM parameter names or release manifests, and secrets
only by name (``secret-ref`` parameters).

Rules (each finding names the rule, file, line and a masked excerpt; the full
match is never printed, so the scan output itself leaks nothing):

=====================  =============================================================
``account-id``         a standalone 12-digit number (also the dashed ``dddd-dddd-dddd`` form)
``arn``                an ARN whose account field is a concrete value (not empty,
                       not ``aws``, not a placeholder)
``s3-bucket``          a bucket name in ``s3://``, an S3 ARN, an S3 virtual-host URL or
                       a ``BucketName``/``bucket``/``bucket_name`` assignment, unless it
                       is an allow-listed placeholder
``endpoint-url``       a deployed endpoint: API Gateway ``execute-api`` host, Lambda
                       function URL, CloudFront distribution host, Cognito user pool ID
``aws-access-key-id``  AWS access key / unique IDs (``AKIA``, ``ASIA``, ``AROA`` ...)
``aws-secret-key``     an ``aws_secret_access_key`` assignment with a 40-char value
``apikey``             ``apikey`` + ``_`` followed by 16+ alphanumerics (third-party API keys)
``private-key``        a PEM private key header
``openai-key``         ``sk-`` / ``sk-proj-`` style keys
``github-token``       ``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_``/``github_pat_`` tokens
``bearer-token``       a literal ``Bearer`` token
``secret-assignment``  a key named like a secret (``secret``, ``password``, ``token``,
                       ``api_key`` ...) assigned a literal value that is neither a
                       placeholder nor a secret *name* (``a/b/c`` path or SSM name)
=====================  =============================================================

Placeholder allow-list: ``<...>`` (for example ``<account-id>``), ``${...}``,
``{{...}}`` and ``{name}`` substitutions, ``*``, ``example-bucket`` and names starting
with ``example``/``placeholder``, AWS documentation keys ending in ``EXAMPLE``, and
key/token values that declare themselves fake (containing ``example``, ``synthetic``,
``placeholder``, ``dummy``, ``fake``, ``not-a-real`` or ``redacted``). Bucket rules only
report syntactically valid bucket names (3-63 characters).
A scanned tree may add regular expressions (one per line, ``#`` comments) in a
``.finplan-leak-allow`` file at its root; a match fully covered by an allow
pattern is not reported.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator

ALLOW_FILE = ".finplan-leak-allow"

#: Directories never scanned (dependencies, build outputs, caches, VCS metadata).
DEFAULT_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "dist",
        "build",
        "cdk.out",
        ".idea",
        ".vscode",
    }
)
#: File suffixes treated as binary and skipped.
BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tgz", ".whl", ".xlsx", ".xls", ".parquet", ".pyc", ".so", ".woff", ".woff2", ".ttf"}
)
MAX_FILE_BYTES = 8 * 1024 * 1024

PLACEHOLDER_BUCKETS = frozenset({"example-bucket"})
PLACEHOLDER_PREFIXES = ("example", "placeholder", "your-", "my-example")

_PLACEHOLDER_RE = re.compile(r"^(?:\*|<[^<>]*>|\$\{[^}]*\}|\{\{[^}]*\}\}|%\([^)]*\)s|\{[A-Za-z_][A-Za-z0-9_]*\})$")
_CONTAINS_PLACEHOLDER_RE = re.compile(r"<[^<>]+>|\$\{[^}]*\}|\{\{[^}]*\}\}|\{[A-Za-z_][A-Za-z0-9_]*\}")


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int
    column: int
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}: [{self.rule}] {self.excerpt}"


def mask(value: str) -> str:
    """Mask a matched value so the report itself never repeats it."""
    if len(value) <= 6:
        return "*" * len(value)
    keep = 2 if len(value) < 16 else 4
    return value[:keep] + "*" * (len(value) - 2 * keep) + value[-keep:]


def is_placeholder(value: str) -> bool:
    v = value.strip().strip("'\"")
    if not v:
        return True
    if _PLACEHOLDER_RE.match(v) or _CONTAINS_PLACEHOLDER_RE.search(v):
        return True
    low = v.lower()
    return low in PLACEHOLDER_BUCKETS or low.startswith(PLACEHOLDER_PREFIXES)


# ----------------------------------------------------------------------- rules
_ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z_.])[0-9]{12}(?![0-9A-Za-z_]|\.[0-9])")
_ACCOUNT_ID_DASHED = re.compile(r"(?<![0-9A-Za-z-])[0-9]{4}-[0-9]{4}-[0-9]{4}(?![0-9A-Za-z-])")
# Inside an ARN the account sits between colons; a 12-digit run there is reported as an ARN.
#: A placeholder substitution, matched as one unit (it may contain ':' or '}').
_PH = r"\$\{[^}]*\}|\{\{[^}]*\}\}|<[^<>\s]*>|\{[A-Za-z_][A-Za-z0-9_.]*\}"
_SEG = rf"(?:{_PH}|[^:\s\"'$<{{}}])*"
_ARN = re.compile(
    rf"arn:(?P<partition>aws[a-z-]*|{_PH}|\*):(?P<service>[A-Za-z0-9*-]+|{_PH}):(?P<region>{_SEG}):(?P<account>{_SEG}):(?P<resource>(?:{_PH}|[^\s\"'`,)\]{{}}])*)"
)
_S3_URI = re.compile(rf"s3a?://(?P<bucket>{_PH}|[^/\s\"'`,)\]{{}}]+)")
_S3_HOST = re.compile(r"(?P<bucket>[a-z0-9][a-z0-9.-]{1,61}[a-z0-9])\.s3(?:[.-](?:[a-z0-9-]+))?\.amazonaws\.com")
_BUCKET_ASSIGN = re.compile(
    r"""(?P<key>["']?(?<![A-Za-z0-9_])(?:BucketName|bucket_name|bucketName|bucket|Bucket|BUCKET(?:_NAME)?)["']?)\s*(?::|=)\s*(?P<q>["'])(?P<bucket>[^"'\s]+)(?P=q)"""
)
#: A syntactically valid S3 bucket name (3-63 chars); shorter tokens are not bucket names.
_VALID_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
#: Values that declare themselves fake are placeholders for the key/token rules.
_SELF_DECLARED_FAKE = ("example", "synthetic", "placeholder", "dummy", "fake", "not-a-real", "notreal", "redacted")
_ENDPOINT_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[a-z0-9]{10}\.execute-api\.[a-z0-9-]+\.amazonaws\.com"),
    re.compile(r"\b[a-z0-9]{32}\.lambda-url\.[a-z0-9-]+\.on\.aws"),
    re.compile(r"\bd[a-z0-9]{12,13}\.cloudfront\.net"),
    re.compile(r"\b(?:us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-[0-9]_[A-Za-z0-9]{9}\b"),
)
_ACCESS_KEY_ID = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16}(?![A-Z0-9])")
_AWS_SECRET = re.compile(r"(?i)aws_?secret_?access_?key[\"']?\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])")
_APIKEY = re.compile(r"(?<![A-Za-z0-9])apikey[_][A-Za-z0-9]{16,}")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----")
_OPENAI_KEY = re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{20,}")
_GITHUB_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")
_BEARER = re.compile(r"(?i)\bbearer\s+(?P<value>[A-Za-z0-9._~+/-]{20,}=*)")
_SECRET_ASSIGN = re.compile(
    r"""(?P<key>["']?[A-Za-z0-9_.-]*(?:secret|password|passwd|pwd|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret)[A-Za-z0-9_.-]*["']?)\s*(?::|=)\s*(?P<q>["'])(?P<value>[^"'\n]*)(?P=q)""",
    re.IGNORECASE,
)
#: A secret *name* or reference (allowed): a path-like or kebab/snake identifier, an SSM name,
#: a secret ARN with a placeholder account, or a runtime lookup.
_SECRET_NAME_RE = re.compile(r"^(?:/?[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)+|[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*)$")
#: Keys whose values are never secrets even though the key matches (token *types*, kinds).
_SECRET_KEY_EXEMPT = re.compile(r"(?i)(?:_|-)?(?:name|names|ref|refs|arn|id|ids|type|kind|key_id|header|field|pattern|regex|env|path|url|uri|hash|digest|prefix|suffix|count|ttl|expires?(?:_at)?|length|class|scope|policy|parameter)$")


def _allowed(value: str, allow: list[re.Pattern[str]]) -> bool:
    return any(p.fullmatch(value) for p in allow)


def is_real_bucket(name: str) -> bool:
    """A concrete, syntactically valid bucket name that is not an allow-listed placeholder."""
    return bool(_VALID_BUCKET.fullmatch(name)) and "*" not in name and not is_placeholder(name)


def _self_declared_fake(value: str) -> bool:
    low = value.lower()
    return any(word in low for word in _SELF_DECLARED_FAKE)


def _arn_account_is_concrete(account: str) -> bool:
    if account in ("", "aws", "*"):
        return False
    return not is_placeholder(account)


def scan_text(text: str, path: str = "<text>", allow: Iterable[re.Pattern[str]] = ()) -> list[Finding]:
    """Scan one text blob; returns findings in line order."""
    allow = list(allow)
    findings: list[Finding] = []
    seen: set[tuple[int, int, str]] = set()

    def add(rule: str, lineno: int, col: int, value: str) -> None:
        if _allowed(value, allow):
            return
        key = (lineno, col, rule)
        if key in seen:
            return
        seen.add(key)
        findings.append(Finding(rule=rule, path=path, line=lineno, column=col + 1, excerpt=mask(value)))

    for lineno, line in enumerate(text.splitlines(), start=1):
        arn_spans: list[tuple[int, int]] = []
        for m in _ARN.finditer(line):
            arn_spans.append(m.span())
            account = m.group("account")
            service = m.group("service")
            if _arn_account_is_concrete(account):
                add("arn", lineno, m.start(), m.group(0))
            elif service == "s3" and m.group("resource"):
                bucket = m.group("resource").split("/", 1)[0]
                if is_real_bucket(bucket):
                    add("s3-bucket", lineno, m.start(), m.group(0))

        def in_arn(pos: int, spans: list[tuple[int, int]] = arn_spans) -> bool:
            return any(a <= pos < b for a, b in spans)

        for m in _ACCOUNT_ID.finditer(line):
            if not in_arn(m.start()):
                add("account-id", lineno, m.start(), m.group(0))
        for m in _ACCOUNT_ID_DASHED.finditer(line):
            add("account-id", lineno, m.start(), m.group(0))
        for m in _S3_URI.finditer(line):
            if is_real_bucket(m.group("bucket")):
                add("s3-bucket", lineno, m.start(), m.group(0))
        for m in _S3_HOST.finditer(line):
            bucket = m.group("bucket")
            if bucket not in ("s3", "www") and is_real_bucket(bucket):
                add("s3-bucket", lineno, m.start(), m.group(0))
        for m in _BUCKET_ASSIGN.finditer(line):
            bucket = m.group("bucket")
            if is_real_bucket(bucket) and not in_arn(m.start("bucket")):
                add("s3-bucket", lineno, m.start("bucket"), bucket)
        for rx in _ENDPOINT_RULES:
            for m in rx.finditer(line):
                add("endpoint-url", lineno, m.start(), m.group(0))
        for m in _ACCESS_KEY_ID.finditer(line):
            if not m.group(0).endswith("EXAMPLE"):
                add("aws-access-key-id", lineno, m.start(), m.group(0))
        for m in _AWS_SECRET.finditer(line):
            if "EXAMPLEKEY" not in m.group("value"):
                add("aws-secret-key", lineno, m.start("value"), m.group("value"))
        for m in _APIKEY.finditer(line):
            if not _self_declared_fake(m.group(0)):
                add("apikey", lineno, m.start(), m.group(0))
        for m in _PRIVATE_KEY.finditer(line):
            add("private-key", lineno, m.start(), m.group(0))
        for m in _OPENAI_KEY.finditer(line):
            if not _self_declared_fake(m.group(0)):
                add("openai-key", lineno, m.start(), m.group(0))
        for m in _GITHUB_TOKEN.finditer(line):
            if not _self_declared_fake(m.group(0)):
                add("github-token", lineno, m.start(), m.group(0))
        for m in _BEARER.finditer(line):
            value = m.group("value")
            if not is_placeholder(value) and not _self_declared_fake(value) and re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value):
                add("bearer-token", lineno, m.start("value"), value)
        for m in _SECRET_ASSIGN.finditer(line):
            key = m.group("key").strip("'\"")
            value = m.group("value")
            if _SECRET_KEY_EXEMPT.search(key) or _is_secret_reference(value):
                continue
            add("secret-assignment", lineno, m.start("value"), value)
    return findings


def _is_secret_reference(value: str) -> bool:
    """True when a value assigned to a secret-like key is a name/reference, not a secret."""
    v = value.strip()
    if len(v) < 8:  # short literals (flags, enum words) are not credentials
        return True
    if is_placeholder(v) or v.lower().startswith(("example", "redacted", "dummy", "fake", "changeme", "test-", "synthetic")):
        return True
    if v.startswith(("arn:", "{{resolve:", "${", "resolve:")):
        return True  # ARNs are checked by the ARN rule; dynamic references resolve at deploy time
    if _SECRET_NAME_RE.match(v):
        return True
    if " " in v:  # prose, messages, descriptions
        return True
    # Must look like a credential: mixed letters and digits, or long base64-ish.
    has_alpha = bool(re.search(r"[A-Za-z]", v))
    has_digit = bool(re.search(r"[0-9]", v))
    return not (has_alpha and has_digit)


# ------------------------------------------------------------------ file walk
def load_allow_patterns(root: Path) -> list[re.Pattern[str]]:
    path = root / ALLOW_FILE
    if not path.is_file():
        return []
    patterns = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            patterns.append(re.compile(line))
    return patterns


def iter_files(root: Path, exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS, exclude_globs: Iterable[str] = ()) -> Iterator[Path]:
    root = Path(root)
    if root.is_file():
        yield root
        return
    exclude_dirs = set(exclude_dirs)
    globs = list(exclude_globs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(rel, g) for g in globs):
                continue
            yield p


def read_text_file(path: Path) -> str | None:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_paths(
    paths: Iterable[str | os.PathLike[str]],
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    exclude_globs: Iterable[str] = (),
    extra_allow: Iterable[str] = (),
) -> tuple[int, list[Finding]]:
    """Scan files and directory trees. Returns (files scanned, findings)."""
    findings: list[Finding] = []
    count = 0
    for given in paths:
        base = Path(given)
        allow = load_allow_patterns(base if base.is_dir() else base.parent) + [re.compile(a) for a in extra_allow]
        for f in iter_files(base, exclude_dirs, exclude_globs):
            text = read_text_file(f)
            if text is None:
                continue
            count += 1
            shown = f.relative_to(base).as_posix() if base.is_dir() else str(f)
            findings.extend(scan_text(text, shown if base.is_dir() else str(f), allow))
    return count, findings


def scan_files(files: Iterable[Path], root: Path | None = None) -> list[Finding]:
    allow = load_allow_patterns(root) if root else []
    out: list[Finding] = []
    for f in files:
        text = read_text_file(Path(f))
        if text is None:
            continue
        shown = Path(f).relative_to(root).as_posix() if root and Path(f).is_relative_to(root) else str(f)
        out.extend(scan_text(text, shown, allow))
    return out


def fixture_check(root: Path, fixture_paths: list[Path]):  # -> list[conformance.Problem]
    """Conformance hook (CS-09 credential/identifier part): scan every contract fixture."""
    from .conformance import Problem  # local import: conformance imports this module lazily

    return [Problem("CS-09", f.path, f"identifier/secret leak: [{f.rule}] {f.excerpt} (line {f.line})") for f in scan_files(fixture_paths, root / "fixtures")]


# ------------------------------------------------------------------------ CLI
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance leak-scan", description="Scan files for account IDs, ARNs, bucket names, endpoints and secrets.")
    ap.add_argument("paths", nargs="*", default=["."], help="files or directories to scan (default: .)")
    ap.add_argument("--files-from", type=Path, help="read the list of files to scan from this file ('-' for stdin), e.g. the output of 'git ls-files'")
    ap.add_argument("--exclude-dir", action="append", default=[], help="additional directory name to skip (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], help="glob (relative to the scanned directory) to skip (repeatable)")
    ap.add_argument("--allow", action="append", default=[], help="additional allow-list regular expression (repeatable)")
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    args = ap.parse_args(argv)

    paths: list[str] = list(args.paths)
    if args.files_from is not None:
        src = sys.stdin.read() if str(args.files_from) == "-" else args.files_from.read_text(encoding="utf-8")
        paths = [line.strip() for line in src.splitlines() if line.strip()]
    count, findings = scan_paths(paths, DEFAULT_EXCLUDE_DIRS | set(args.exclude_dir), args.exclude, args.allow)
    if args.json:
        print(json.dumps({"files_scanned": count, "ok": not findings, "findings": [asdict(f) for f in findings]}, indent=2, sort_keys=True))
    else:
        for f in findings:
            print(f)
        print(f"{'PASS' if not findings else 'FAIL'}: leak scan, {count} files, {len(findings)} findings")
    return 0 if not findings else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))

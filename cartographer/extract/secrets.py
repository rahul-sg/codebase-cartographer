"""
Credential detection that never stores the credential.

The stack reference calls for this explicitly, having found a hardcoded
Confluent Cloud SASL credential in `agent/src/main/resources/application.properties`
and deliberately not recording its value anywhere.

This module follows the same rule, structurally rather than by good intentions:
the matched secret is never written to the graph, never returned, and never
logged. Only the file, the line, and the KIND of secret are recorded. A
fingerprint (first 8 hex of a salted SHA-256) is kept so the same finding can be
recognised across scans without the value being recoverable from the database.

The salt is random per scan, so fingerprints are stable within one graph and
meaningless outside it.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets as _pysecrets

from ..config import SKIP_DIRS

SOURCE = "secrets"

SCAN_EXT = (".properties", ".yml", ".yaml", ".env", ".conf", ".cfg", ".ini",
            ".json", ".xml", ".tf", ".tfvars", ".sh", ".java", ".kt", ".py",
            ".js", ".ts")
SCAN_NAMES = (".env", "Dockerfile", "docker-compose.yml", "docker-compose.yaml")

# Each pattern captures the secret in group 1 or the last group. The value is
# used only to compute a fingerprint and is then discarded.
PATTERNS = [
    ("aws-access-key-id", re.compile(r'\b(AKIA[0-9A-Z]{16})\b')),
    ("private-key-block", re.compile(r'(-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----)')),
    ("jwt", re.compile(r'\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})')),
    ("openai-key", re.compile(r'\b(sk-[A-Za-z0-9_\-]{20,})')),
    ("anthropic-key", re.compile(r'\b(sk-ant-[A-Za-z0-9_\-]{20,})')),
    ("google-api-key", re.compile(r'\b(AIza[0-9A-Za-z_\-]{35})\b')),
    ("slack-token", re.compile(r'\b(xox[abposr]-[0-9A-Za-z\-]{10,})')),
    ("github-token", re.compile(r'\b(gh[pousr]_[0-9A-Za-z]{36,})')),
    ("sasl-jaas-password", re.compile(
        r'PlainLoginModule\s+required[^;\n]*?password\s*=\s*["\']([^"\'\s;]{8,})["\']', re.I)),
    ("basic-auth-in-url", re.compile(r'://[^/\s:@"\']{2,}:([^/\s:@"\']{6,})@')),
    ("assigned-password", re.compile(
        r'(?:^|[\s.,;\[{])(?:password|passwd|pwd|secret|token|api[_.-]?key|'
        r'access[_.-]?key|client[_.-]?secret|auth[_.-]?token|private[_.-]?key)'
        r'\s*[:=]\s*["\']?([^\s"\'{}<>,;]{8,120})["\']?', re.I)),
]

# Values that look like secrets but are placeholders, references, or defaults.
BENIGN = re.compile(
    r'^(?:'
    r'\$\{.*\}|\$\(.*\)|%\(.*\)s|\{\{.*\}\}|<.*>|'          # interpolation
    r'(?:changeme|change_me|changeit|password|secret|token|null|none|nil|'
    r'true|false|example|sample|dummy|test|foo|bar|xxx+|yyy+|zzz+|todo|'
    r'placeholder|redacted|removed|your[_-]?\w*|my[_-]?\w*|abc123|'
    r'\*+|\.+|-+|0+|1+)'
    r')$', re.I)
BENIGN_SUFFIX = re.compile(r'(\.enabled|\.enable|\.timeout|\.port|\.class|\.url|\.name)$', re.I)

TEST_PATH = re.compile(r'(^|/)(test|tests|spec|__tests__|fixtures?|mocks?|examples?)(/|$)', re.I)


def _looks_benign(value, key_context=""):
    v = (value or "").strip()
    if len(v) < 8 or len(v) > 200:
        return True
    if BENIGN.match(v):
        return True
    if v.startswith(("${", "$(", "{{", "<%")) or v.endswith(("}", ")")):
        return True
    if BENIGN_SUFFIX.search(key_context):
        return True
    # A value with no variety is almost certainly a placeholder.
    if len(set(v)) <= 3:
        return True
    return False


def _fingerprint(value, salt):
    return hashlib.sha256((salt + value).encode("utf-8", "replace")).hexdigest()[:8]


def _walk(repo_root, follow=False, cap=2_000_000):
    for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=follow):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.endswith(SCAN_EXT) or fn in SCAN_NAMES or fn.startswith(".env"):
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(p) > cap:
                        continue
                except OSError:
                    continue
                yield p


def run(store, cfg, repos, progress=None):
    """
    Records findings as gaps only. No node, no edge, and above all no value
    ever enters the graph.
    """
    store.conn.execute("DELETE FROM gaps WHERE source=?", (SOURCE,))
    salt = _pysecrets.token_hex(16)
    follow = cfg.defaults.get("follow_symlinks", False)
    findings = []
    seen = set()

    for repo_name, repo_root, svc in repos:
        for path in _walk(repo_root, follow):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().split("\n")
            except OSError:
                continue
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            in_test = bool(TEST_PATH.search(rel))
            for i, line in enumerate(lines, start=1):
                if len(line) > 4000:
                    continue
                stripped = line.strip()
                if stripped.startswith(("#", "//", "*", "<!--")):
                    continue
                for kind, pat in PATTERNS:
                    m = pat.search(line)
                    if not m:
                        continue
                    value = m.group(m.lastindex or 1)
                    key_ctx = line[:m.start(m.lastindex or 1)][-60:]
                    if _looks_benign(value, key_ctx):
                        continue
                    fp = _fingerprint(value, salt)
                    key = (kind, fp)
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append({
                        "kind": kind, "repo": repo_name, "path": rel,
                        "line": i, "fingerprint": fp, "in_test": in_test,
                    })
                    break                      # one finding per line is enough
                # `value` goes out of scope here and is never persisted.

    real = [f for f in findings if not f["in_test"]]
    test = [f for f in findings if f["in_test"]]

    for f in real:
        store.add_gap(
            "credential-in-source",
            "%s: possible %s at %s/%s:%d [fingerprint %s]"
            % (f["repo"], f["kind"], f["repo"], f["path"], f["line"], f["fingerprint"]),
            "the value was NOT recorded. Verify by opening that line yourself; "
            "if real, rotate it and move it to a secrets manager. Never paste "
            "the value into a prompt, a ticket, or a chat.",
            SOURCE)
    if test:
        store.add_gap(
            "credential-in-test-fixture",
            "%d credential-shaped strings in test/fixture paths" % len(test),
            "usually harmless, but confirm none are real: "
            + ", ".join(sorted({"%s/%s" % (f["repo"], f["path"]) for f in test})[:8]),
            SOURCE)
    store.commit()
    # Deliberately returns counts and locations only.
    return {"findings": len(real), "in_test_paths": len(test),
            "files_flagged": len({f["path"] for f in real})}

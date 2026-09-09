"""
Acknowledging findings, so the lists can shrink.

A scan of this estate produces ~1,750 gaps, 108 credential locations and 65
multiple-writer tables. None of that is wrong, and none of it is actionable in
one sitting. A list that only ever grows stops being read at all -- so the tool
needs a way to record "looked at this, here is why it stays", and then get out
of the way.

Two design points matter more than the mechanics:

**The record is committed, not local.** `.cartographer-ack.yaml` sits next to
`cartographer.yaml` and is meant to be in git. An acknowledgement carries a
reason and a name; it is a small architecture decision log, and its value is
that the next person sees why a shared table has two writers on purpose rather
than rediscovering it and filing the same question.

**Keys survive code moving.** A finding is identified by category plus its
location with the line number stripped, so an ack does not evaporate the moment
somebody inserts an import above it. It is still specific: a different file, or
a different kind of finding in the same file, is a different key.

Nothing is ever deleted from the graph. Acknowledged findings are hidden from
the default view and shown by `--all`, because suppressing evidence outright is
how a tool starts lying by omission.
"""
from __future__ import annotations

import hashlib
import os
import re

ACK_NAME = ".cartographer-ack.yaml"

# `repo/path/File.java:63` -> `repo/path/File.java`. Line numbers drift for
# reasons that have nothing to do with the finding.
_LINE_SUFFIX = re.compile(r":(\d+)(?=\b|$)")
# Counts inside a detail string ("3 services", "12 further") change as the
# codebase grows without changing what is being reported.
_NUMBERS = re.compile(r"\b\d+\b")


def _normalise(detail):
    d = (detail or "").strip()
    d = _LINE_SUFFIX.sub("", d)
    d = _NUMBERS.sub("#", d)
    return re.sub(r"\s+", " ", d).lower()


def key_for(category, detail):
    """
    Stable 8-hex identifier for one finding.

    Derived from the category and the location-with-line-stripped, so it is
    reproducible across scans and across machines -- which is what makes a
    committed ack file work for a whole team rather than one laptop.
    """
    basis = "%s|%s" % ((category or "").strip().lower(), _normalise(detail))
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:8]


def ack_path(cfg):
    base = os.path.dirname(cfg.path) if getattr(cfg, "path", None) else None
    if not base:
        base = (cfg.roots or [os.getcwd()])[0]
    return os.path.join(base, ACK_NAME)


def load(path):
    """
    `key -> {reason, by, date, note}`.

    Parsed with a deliberately small reader rather than the shared YAML
    loader: this file is written by this module, its shape is fixed, and a
    malformed hand-edit should degrade to "nothing is acknowledged" instead of
    failing a scan.
    """
    out = {}
    if not path or not os.path.exists(path):
        return out
    cur = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                m = re.match(r"^\s{0,2}([0-9a-f]{8})\s*:\s*$", line)
                if m:
                    cur = m.group(1)
                    out[cur] = {}
                    continue
                m = re.match(r"^\s+(reason|by|date|note|category)\s*:\s*(.*)$",
                             line)
                if m and cur:
                    val = m.group(2).strip().strip('"').strip("'")
                    out[cur][m.group(1)] = val
    except OSError:
        return {}
    return out


def save(path, acks):
    """Rewrite the ack file. Sorted, so diffs stay readable in review."""
    lines = [
        "# cartographer -- acknowledged findings.",
        "# Commit this file. Each entry records a finding that has been looked",
        "# at and consciously accepted, with the reason it stays.",
        "#",
        "# Acknowledged findings are hidden from the default output and shown",
        "# by `--all`. Nothing is removed from the graph itself.",
        "",
    ]
    for k in sorted(acks):
        e = acks[k] or {}
        lines.append("%s:" % k)
        for field in ("category", "reason", "by", "date", "note"):
            if e.get(field):
                val = str(e[field]).replace('"', "'")
                lines.append('  %s: "%s"' % (field, val))
        lines.append("")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    os.replace(tmp, path)


def split(rows, acks):
    """
    Partition `[(category, detail, ...)]` into (visible, acknowledged).

    Rows are passed through untouched so callers keep whatever extra fields
    they selected; only the first two positions are inspected.
    """
    visible, hidden = [], []
    for row in rows:
        cat = row[0] if len(row) > 0 else ""
        det = row[1] if len(row) > 1 else ""
        (hidden if key_for(cat, det) in acks else visible).append(row)
    return visible, hidden

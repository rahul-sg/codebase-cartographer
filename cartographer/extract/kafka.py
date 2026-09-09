"""
Kafka topology where topic names are CONSTANTS, not string literals.

On this estate, no service names a topic inline. Names are computed at runtime:

    topicNameCreator.createTopicName(KafkaConstants.KafkaTopicName.ORDER_SUBMITTED)

and listeners reference the same enum through a SpEL expression. A regex
looking for quoted topic strings finds nothing, and the local docker-compose
topic list is stale relative to the code -- so the naive answer is not merely
incomplete, it is wrong.

This extractor works in two passes:

  1. Build a constant table from every enum / static-final that maps an
     identifier to a topic-shaped string.
  2. Find references to those identifiers and classify each as produce or
     consume from its surrounding context.

An unresolved direction is recorded as a mention rather than guessed, because a
backwards event edge is worse than a missing one.
"""
from __future__ import annotations

import os
import re

from .. import ids, langs
from ..config import SKIP_DIRS, prune

SOURCE = "kafka"

# ENUM_MEMBER("some.topic.name")
ENUM_CONST = re.compile(
    r'\b([A-Z][A-Z0-9_]{2,60})\s*\(\s*"([^"\n]{3,200})"')
# public static final String X = "some.topic";
STATIC_CONST = re.compile(
    r'\b(?:public\s+|private\s+|protected\s+|static\s+|final\s+)*'
    r'String\s+([A-Z][A-Z0-9_]{2,60})\s*=\s*"([^"\n]{3,200})"')
# A reference to a known constant.
REF = re.compile(r'\b([A-Z][A-Z0-9_]{2,60})\b')

LISTENER_ANN = re.compile(
    r'@(?:KafkaListener|StreamListener|RabbitListener|JmsListener|'
    r'SqsListener|Incoming|KafkaHandler)\b', re.I)
PRODUCE_HINT = re.compile(
    r'\.(?:send|publish|produce|emit|sendDefault|sendMessage|publishEvent)\s*\(', re.I)
CREATE_TOPIC = re.compile(r'createTopicName\s*\(', re.I)

# A topic name looks like a dotted or dashed path, not a sentence or a SQL
# fragment. Without this, every constant string in the codebase becomes a topic.
TOPIC_SHAPE = re.compile(r'^[a-z0-9][a-z0-9_]*(?:[.\-][a-z0-9_]+){1,6}$', re.I)

CONST_FILE_HINT = re.compile(r'(kafka|topic|event|messaging|stream)', re.I)


def _read(path, limit=1_500_000):
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _java_files(repo_root, follow=False):
    for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=follow):
        prune(dirpath, dirnames)
        for fn in sorted(filenames):
            if fn.endswith((".java", ".kt", ".scala", ".groovy")):
                yield os.path.join(dirpath, fn)


def collect_constants(repos, follow=False):
    """
    identifier -> {"topic": str, "evidence": "repo/path:line"}

    Only files whose name or content suggests messaging are mined, so an
    unrelated enum of status codes does not become a topic table.
    """
    table = {}
    for repo_name, repo_root, _svc in repos:
        for path in _java_files(repo_root, follow):
            base = os.path.basename(path)
            text = _read(path)
            if text is None:
                continue
            if not CONST_FILE_HINT.search(base) and "Kafka" not in text[:4000]:
                continue
            clean = langs.strip_noise(text, "java")
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            # strip_noise blanks string literals, so mine the raw text for the
            # values and use the cleaned copy only to skip commented-out code.
            clean_lines = clean.split("\n")
            for i, line in enumerate(text.split("\n"), start=1):
                if i - 1 < len(clean_lines) and not clean_lines[i - 1].strip():
                    continue                       # whole line was a comment
                for pat in (ENUM_CONST, STATIC_CONST):
                    for m in pat.finditer(line):
                        ident, value = m.group(1), m.group(2).strip()
                        if not TOPIC_SHAPE.match(value):
                            continue
                        table.setdefault(ident, {
                            "topic": value,
                            "evidence": "%s/%s:%d" % (repo_name, rel, i),
                            "declared_in": repo_name})
    return table


def _listener_spans(text):
    """Character ranges covered by a listener annotation and its arguments."""
    spans = []
    for m in LISTENER_ANN.finditer(text):
        i = m.end()
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] != "(":
            spans.append((m.start(), min(len(text), m.end() + 200)))
            continue
        depth, j = 0, i
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        spans.append((m.start(), min(j + 1, len(text))))
    return spans


def run(store, cfg, repos, progress=None):
    store.clear_source(SOURCE)
    follow = cfg.defaults.get("follow_symlinks", False)

    table = collect_constants(repos, follow)
    if progress:
        progress("  kafka: %d topic constants" % len(table))

    produces = {}       # topic -> {service: evidence}
    consumes = {}
    mentions = {}
    nodes, edges = [], []

    for repo_name, repo_root, svc_name in repos:
        me = svc_name or repo_name
        for path in _java_files(repo_root, follow):
            text = _read(path)
            if text is None:
                continue
            if not any(k in text for k in table):
                continue
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            here = "%s/%s" % (repo_name, rel)
            declared_here = {k for k, v in table.items()
                             if v["evidence"].rsplit(":", 1)[0] == here}
            # Which service is this file in? A monorepo of modules means the
            # repo name is too coarse.
            owner = _owner_for(cfg, repo_root, path, me)
            spans = _listener_spans(text)
            clean = langs.strip_noise(text, "java")
            clean_lines = clean.split("\n")

            offset = 0
            for i, line in enumerate(text.split("\n"), start=1):
                start, end = offset, offset + len(line)
                offset = end + 1
                if i - 1 < len(clean_lines) and not clean_lines[i - 1].strip():
                    continue
                refs = [r for r in REF.findall(line)
                        if r in table and r not in declared_here]
                if not refs:
                    continue
                # Overlap, not containment. An annotation begins partway into
                # its line, so a containment test never matches.
                in_listener = any(start < e and s < end for s, e in spans)
                ev = "%s/%s:%d" % (repo_name, rel, i)
                for ident in refs:
                    topic = table[ident]["topic"]
                    if in_listener:
                        consumes.setdefault(topic, {}).setdefault(owner, ev)
                    elif PRODUCE_HINT.search(line) or CREATE_TOPIC.search(line):
                        # createTopicName inside a send is the idiom on this
                        # estate; a bare createTopicName call is still
                        # producer-side setup.
                        produces.setdefault(topic, {}).setdefault(owner, ev)
                    else:
                        mentions.setdefault(topic, {}).setdefault(owner, ev)

    all_topics = set(produces) | set(consumes) | set(mentions)
    ident_by_topic = {v["topic"]: (k, v) for k, v in table.items()}

    for topic in sorted(all_topics):
        ident, meta = ident_by_topic.get(topic, (None, {}))
        tid = "cart . topic %s" % topic
        nodes.append({"id": tid, "kind": "topic", "name": topic,
                      "extra": {"constant": ident,
                                "declared_at": meta.get("evidence"),
                                "runtime_resolved": True}})
        for p, ev in produces.get(topic, {}).items():
            nodes.append({"id": ids.service_id(p), "kind": "service",
                          "name": p, "service": p})
            edges.append({"src": ids.service_id(p), "dst": tid,
                          "kind": "produces", "evidence": ev,
                          "provenance": "EXTRACTED", "confidence": 0.9,
                          "extra": {"via": "constant %s" % ident}})
        for c, ev in consumes.get(topic, {}).items():
            nodes.append({"id": ids.service_id(c), "kind": "service",
                          "name": c, "service": c})
            edges.append({"src": tid, "dst": ids.service_id(c),
                          "kind": "consumed-by", "evidence": ev,
                          "provenance": "EXTRACTED", "confidence": 0.9,
                          "extra": {"via": "constant %s" % ident}})
        for p in produces.get(topic, {}):
            for c in consumes.get(topic, {}):
                if p == c:
                    continue
                edges.append({"src": ids.service_id(p), "dst": ids.service_id(c),
                              "kind": "event",
                              "evidence": produces[topic][p],
                              "provenance": "EXTRACTED", "confidence": 0.85,
                              "extra": {"via": "topic %s (constant %s)"
                                        % (topic, ident)}})
        if topic in produces and topic not in consumes:
            store.add_gap("unconsumed-topic",
                          "topic '%s' (constant %s) is produced by %s but no "
                          "consumer was found"
                          % (topic, ident, ", ".join(sorted(produces[topic]))),
                          "the consumer may be in a repo you do not have, or "
                          "subscribe dynamically", SOURCE)
        if topic in consumes and topic not in produces:
            store.add_gap("unproduced-topic",
                          "topic '%s' (constant %s) is consumed by %s but no "
                          "producer was found"
                          % (topic, ident, ", ".join(sorted(consumes[topic]))),
                          "the producer may be in a repo you do not have",
                          SOURCE)
        only_mentioned = (topic in mentions and topic not in produces
                          and topic not in consumes)
        if only_mentioned:
            store.add_gap("topic-direction-unknown",
                          "topic '%s' (constant %s) is referenced by %s but "
                          "neither a send nor a listener was identified"
                          % (topic, ident, ", ".join(sorted(mentions[topic]))),
                          "read the reference directly to determine direction",
                          SOURCE)

    unused = [k for k, v in table.items()
              if v["topic"] not in all_topics]
    for ident in sorted(unused)[:40]:
        store.add_gap("topic-constant-unreferenced",
                      "topic constant %s ('%s') is declared but never "
                      "referenced in the repos on disk"
                      % (ident, table[ident]["topic"]),
                      "declared at %s -- its user may be a repo you do not have"
                      % table[ident]["evidence"], SOURCE)

    store.add_nodes(nodes, SOURCE)
    store.add_edges(edges, SOURCE)
    store.commit()
    return {"constants": len(table), "topics": len(all_topics),
            "produces": sum(len(v) for v in produces.values()),
            "consumes": sum(len(v) for v in consumes.values()),
            "unresolved_direction": len(mentions),
            "unreferenced_constants": len(unused)}


def _owner_for(cfg, repo_root, path, fallback):
    """
    Attribute a file to its Maven module when the repo holds many.

    <repo>/server/<module>/... -- the module directory is the service,
    not the repo.
    """
    rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
    parts = rel.split("/")
    for i, seg in enumerate(parts[:-1]):
        if seg in ("server", "modules", "services", "apps"):
            if i + 1 < len(parts) - 1:
                cand = cfg.resolve_service(parts[i + 1])
                return cand or parts[i + 1]
    cand = cfg.resolve_service(parts[0]) if len(parts) > 1 else None
    return cand or fallback

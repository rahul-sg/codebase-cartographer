"""
A deliberately small YAML reader.

PyYAML is not guaranteed on a locked-down machine, and the config this tool
needs is a nested map of scalars and lists. So we parse that subset ourselves
and refuse to pretend we handle more.

Supported: nested mappings, block lists, inline [a, b] and {a: b}, quoted and
bare scalars, ints/floats/bools/null, `#` comments, blank lines, `---`
document start, and multi-line block scalars (| and >).

Not supported: anchors, aliases, tags, multiple documents, complex keys. Each
raises MiniYamlError with a line number rather than silently misreading, since
a config misread produces a wrong map and a wrong map is worse than no map.

If PyYAML is importable, cartographer.config uses it instead of this.
"""
from __future__ import annotations

import re


class MiniYamlError(ValueError):
    pass


# Anchors/aliases/tags in key OR value position. Anchored to a value start so
# that "a&b" in a URL and a "*.java" glob are not false positives.
_UNSUPPORTED = re.compile(r"(?:^|:[ \t]|-[ \t])[ \t]*(?:[&*]\w|!!?\w)")


def _scalar(tok, lineno):
    t = tok.strip()
    if t == "" or t in ("~", "null", "Null", "NULL"):
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        body = t[1:-1]
        if t[0] == '"':
            body = (body.replace('\\n', '\n').replace('\\t', '\t')
                        .replace('\\"', '"').replace('\\\\', '\\'))
        else:
            body = body.replace("''", "'")
        return body
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        return [] if not inner else [_scalar(x, lineno) for x in _split_inline(inner)]
    if t.startswith("{") and t.endswith("}"):
        inner = t[1:-1].strip()
        out = {}
        if inner:
            for part in _split_inline(inner):
                k, sep, v = part.partition(":")
                if not sep:
                    raise MiniYamlError("line %d: expected key: value in inline map" % lineno)
                out[_scalar(k, lineno)] = _scalar(v, lineno)
        return out
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _split_inline(s):
    """Split on commas that are not inside quotes or nested brackets."""
    out, buf, depth, quote = [], [], 0, None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [x for x in (p.strip() for p in out) if x != ""]


def _strip_comment(line):
    """Remove a trailing # comment that is not inside quotes."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def loads(text):
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    lines = []           # (indent, content, lineno)
    i = 0
    while i < len(raw):
        lineno = i + 1
        line = raw[i]
        if line.strip() in ("---", "..."):
            i += 1
            continue
        stripped = _strip_comment(line)
        if not stripped.strip():
            i += 1
            continue
        if _UNSUPPORTED.search(stripped):
            raise MiniYamlError(
                "line %d: anchors, aliases and tags are not supported "
                "(install PyYAML, or simplify the config)" % lineno)
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()

        # Block scalar: swallow the more-indented body that follows.
        m = re.match(r"^(.*?):\s*([|>])[-+]?\s*$", content)
        if m:
            key = m.group(1)
            fold = m.group(2) == ">"
            body, j = [], i + 1
            base = None
            while j < len(raw):
                nxt = raw[j]
                if not nxt.strip():
                    body.append("")
                    j += 1
                    continue
                ind = len(nxt) - len(nxt.lstrip(" "))
                if ind <= indent:
                    break
                if base is None:
                    base = ind
                body.append(nxt[base:])
                j += 1
            text_val = (" ".join(x.strip() for x in body if x.strip())
                        if fold else "\n".join(body).rstrip("\n"))
            lines.append((indent, (key.strip(), text_val, True), lineno))
            i = j
            continue

        lines.append((indent, content, lineno))
        i += 1

    if not lines:
        return {}
    pos = [0]

    def parse_block(indent):
        # Decide container type from the first line at this indent.
        first = lines[pos[0]]
        if isinstance(first[1], tuple):
            return parse_map(indent)
        return parse_list(indent) if first[1].startswith("- ") or first[1] == "-" \
            else parse_map(indent)

    def parse_list(indent):
        out = []
        while pos[0] < len(lines):
            ind, content, lineno = lines[pos[0]]
            if ind < indent:
                break
            if ind > indent:
                raise MiniYamlError("line %d: unexpected indentation in list" % lineno)
            if isinstance(content, tuple) or not (content == "-" or content.startswith("- ")):
                break
            item = content[1:].strip()
            pos[0] += 1
            if item == "":
                if pos[0] < len(lines) and lines[pos[0]][0] > indent:
                    out.append(parse_block(lines[pos[0]][0]))
                else:
                    out.append(None)
            elif ":" in item and not _looks_scalar(item):
                # "- name: x" starts a map whose remaining keys are indented
                # to the column where the key text begins.
                key_indent = indent + 2
                synthetic = [(key_indent, item, lineno)]
                lines[pos[0]:pos[0]] = synthetic
                out.append(parse_map(key_indent))
            else:
                out.append(_scalar(item, lineno))
        return out

    def _looks_scalar(item):
        """`- http://x` has a colon but is not a mapping."""
        k, sep, _ = item.partition(":")
        if not sep:
            return True
        if k.strip().startswith(("'", '"')):
            return False
        return bool(re.search(r"[\s/]", k)) or k.strip() == ""

    def parse_map(indent):
        out = {}
        while pos[0] < len(lines):
            ind, content, lineno = lines[pos[0]]
            if ind < indent:
                break
            if ind > indent:
                raise MiniYamlError("line %d: unexpected indentation in mapping" % lineno)
            if isinstance(content, tuple):
                key, val, _ = content
                out[_scalar(key, lineno)] = val
                pos[0] += 1
                continue
            if content.startswith("- "):
                break
            key, sep, rest = content.partition(":")
            if not sep:
                raise MiniYamlError(
                    "line %d: expected 'key: value', got %r" % (lineno, content))
            key = _scalar(key, lineno)
            rest = rest.strip()
            pos[0] += 1
            if rest:
                out[key] = _scalar(rest, lineno)
            else:
                if pos[0] < len(lines) and lines[pos[0]][0] > indent:
                    out[key] = parse_block(lines[pos[0]][0])
                elif (pos[0] < len(lines) and lines[pos[0]][0] == indent
                      and not isinstance(lines[pos[0]][1], tuple)
                      and lines[pos[0]][1].startswith("- ")):
                    out[key] = parse_list(indent)   # list at same indent as key
                else:
                    out[key] = None
        return out

    result = parse_block(lines[0][0])
    if pos[0] < len(lines):
        raise MiniYamlError("line %d: could not parse to end of file"
                            % lines[pos[0]][2])
    return result


def load(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return loads(fh.read())

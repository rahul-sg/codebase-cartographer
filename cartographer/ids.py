"""
Stable symbol identifiers, modelled on Sourcegraph's SCIP scheme.

Why this exists: the naive approach keys symbols by bare name, which collides
constantly across a 15-service estate ("validate" exists everywhere) and forces
you to drop ambiguous matches. SCIP's insight is that a symbol ID should encode
its full location, so two symbols with the same name in different packages are
simply different IDs and never need disambiguating at query time.

Format (a simplified SCIP descriptor):

    <scheme> <repo> <lang> <path>`<container>#<name>().

Examples:
    cart order-svc java src/OrderService.java`OrderService#submitOrder().
    cart ml-svc python src/matcher.py`ProductMatcher#predict_batch().
    cart order-svc java src/OrderService.java`OrderService#

These are deterministic, human-readable, stable across runs, and comparable
across repos.
"""
from __future__ import annotations

SCHEME = "cart"


def _clean(s):
    return (s or "").replace("`", "'").replace("\n", " ").strip()


def file_id(repo, lang, path):
    """Identifier for a source file."""
    return "{} {} {} {}`".format(SCHEME, _clean(repo), _clean(lang), _clean(path))


def symbol_id(repo, lang, path, name, kind, container=None):
    """
    Identifier for a definition inside a file.

    `container` is the enclosing class/struct/module when known. Functions get a
    trailing "()." so a method and a same-named field never collide.
    """
    base = "{} {} {} {}`".format(SCHEME, _clean(repo), _clean(lang), _clean(path))
    if container:
        base += "{}#".format(_clean(container))
    name = _clean(name)
    if kind in ("function", "method"):
        return base + name + "()."
    if kind in ("class", "interface", "struct", "trait", "enum"):
        return base + name + "#"
    return base + name + "."


def external_id(module):
    """A dependency we can see referenced but whose source we do not have."""
    return "{} . external {}".format(SCHEME, _clean(module))


def service_id(name):
    return "{} . service {}".format(SCHEME, _clean(name))


def parse(sid):
    """
    Split an id back into its parts. Returns a dict; missing parts are None.
    Tolerant by design — never raises on malformed input, because ids can come
    from a stale graph file written by an older version.
    """
    out = {"scheme": None, "repo": None, "lang": None, "path": None,
           "container": None, "name": None, "kind": None}
    if not isinstance(sid, str):
        return out
    head, sep, tail = sid.partition("`")
    parts = head.split(" ", 3)
    if len(parts) >= 1:
        out["scheme"] = parts[0]
    if len(parts) >= 2:
        out["repo"] = parts[1]
    if len(parts) >= 3:
        out["lang"] = parts[2]
    if len(parts) >= 4:
        out["path"] = parts[3]
    if not sep:
        return out
    if not tail:
        out["kind"] = "file"
        return out
    container, hsep, rest = tail.partition("#")
    if hsep and rest:
        out["container"] = container
        tail = rest
    elif hsep and not rest:
        out["name"] = container
        out["kind"] = "class"
        return out
    if tail.endswith("()."):
        out["name"] = tail[:-3]
        out["kind"] = "function"
    elif tail.endswith("#"):
        out["name"] = tail[:-1]
        out["kind"] = "class"
    elif tail.endswith("."):
        out["name"] = tail[:-1]
        out["kind"] = "symbol"
    else:
        out["name"] = tail
    return out


def display(sid):
    """Short human-readable form for reports."""
    p = parse(sid)
    if p["name"]:
        owner = (p["container"] + ".") if p["container"] else ""
        return "{}{}".format(owner, p["name"])
    if p["path"]:
        return p["path"]
    return sid

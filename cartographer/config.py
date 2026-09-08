"""
Configuration loading and service-identity resolution.

The single hardest problem in mapping a microservice estate is not parsing --
it is deciding that `order-svc`, `orders`, `OrderService`, `ORDER_SERVICE_URL`
and `oms/order-svc:latest` all name the same thing. That aliasing logic lives
here so every extractor resolves names identically.
"""
from __future__ import annotations

import os
import re
import json

try:                                   # prefer the real thing when present
    import yaml as _yaml               # type: ignore
    _HAVE_YAML = True
except Exception:                      # pragma: no cover - environment dependent
    _yaml = None
    _HAVE_YAML = False

from . import miniyaml

DEFAULT_CONFIG_NAMES = ("cartographer.yaml", "cartographer.yml",
                        "config/services.yaml", "services.yaml")
OUT_DIR_NAME = ".cartographer"

DEFAULTS = {
    "history_months": 12,
    "max_file_bytes": 2_000_000,
    "min_coupling_shared": 3,
    "min_coupling_degree": 0.30,
    "max_commit_files": 40,      # commits touching more files are formatting
                                 # sweeps or merges; they poison coupling
    "call_edges": True,
    "follow_symlinks": False,
}

SKIP_DIRS = {
    "node_modules", ".git", ".hg", ".svn", "target", "build", "out", "dist",
    "vendor", ".venv", "venv", "env", "__pycache__", ".gradle", ".idea",
    ".vscode", "coverage", ".mvn", ".next", ".nuxt", ".terraform",
    "bower_components", "site-packages", ".tox", ".pytest_cache", ".mypy_cache",
    "bin", "obj", "Pods", "DerivedData", ".serverless", "cdk.out",
}

# Directory names that are almost always generated code. Kept separate from
# SKIP_DIRS because a repo may legitimately have a `gen` package.
GENERATED_HINTS = ("generated", "gen-src", "build-gen", "__generated__",
                   ".generated", "target/generated-sources")


class ConfigError(Exception):
    pass


class Config(object):
    def __init__(self, data, path=None, base_dir=None):
        self.path = path
        self.raw = data or {}
        self.base_dir = os.path.abspath(base_dir or (os.path.dirname(path) if path else os.getcwd()))

        d = dict(DEFAULTS)
        d.update(self.raw.get("defaults") or {})
        self.defaults = d

        self.roots = [_expand(r) for r in (self.raw.get("roots") or []) if r]
        self.services = []
        for s in (self.raw.get("services") or []):
            if not isinstance(s, dict) or not s.get("name"):
                continue
            svc = dict(s)
            if svc.get("repo"):
                svc["repo"] = _expand(svc["repo"])
            svc["aliases"] = [a for a in (svc.get("aliases") or []) if a]
            svc["owns_data"] = svc.get("owns_data") or []
            self.services.append(svc)

        self.flows = [f for f in (self.raw.get("flows") or []) if f]
        self.out_dir = _expand(self.raw.get("out_dir")
                               or os.path.join(self.base_dir, OUT_DIR_NAME))
        self.exclude = list(self.raw.get("exclude") or [])
        self._alias_map = None

    # -- service identity --------------------------------------------------

    def alias_map(self):
        """
        token -> canonical service name.

        Tokens are generated aggressively (case-folded, separators stripped,
        common suffixes dropped) because the same service is written a dozen
        ways across Java constants, YAML keys and Docker image tags.
        """
        if self._alias_map is not None:
            return self._alias_map
        m = {}
        for svc in self.services:
            canon = svc["name"]
            tokens = {canon}
            tokens.update(svc["aliases"])
            if svc.get("repo"):
                tokens.add(os.path.basename(svc["repo"].rstrip("/")))
            for t in list(tokens):
                tokens.update(_token_variants(t))
            for t in tokens:
                key = _norm_token(t)
                if not key:
                    continue
                # First writer wins, so an explicit alias is never displaced by
                # a generated variant of some other service.
                m.setdefault(key, canon)
        self._alias_map = m
        return m

    def resolve_service(self, token):
        """Canonical service name for an arbitrary token, or None."""
        if not token:
            return None
        m = self.alias_map()
        key = _norm_token(token)
        if key in m:
            return m[key]
        for variant in _token_variants(token):
            k = _norm_token(variant)
            if k in m:
                return m[k]
        return None

    def service_for_repo(self, repo_name):
        for svc in self.services:
            if svc.get("repo") and os.path.basename(svc["repo"].rstrip("/")) == repo_name:
                return svc["name"]
        return self.resolve_service(repo_name)

    def service(self, name):
        for svc in self.services:
            if svc["name"] == name:
                return svc
        return None

    # -- paths -------------------------------------------------------------

    def db_path(self):
        return os.path.join(self.out_dir, "graph.db")

    def out(self, *parts):
        return os.path.join(self.out_dir, *parts)

    def ensure_out(self):
        os.makedirs(self.out_dir, exist_ok=True)
        return self.out_dir

    def to_dict(self):
        return {"path": self.path, "roots": self.roots, "out_dir": self.out_dir,
                "services": self.services, "flows": self.flows,
                "defaults": self.defaults}


# -- helpers ---------------------------------------------------------------

_SUFFIXES = ("service", "svc", "server", "api", "app", "srv", "ms")


def _expand(p):
    if not p:
        return p
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(p))))


def _norm_token(t):
    """Case-fold and strip separators: ORDER_SERVICE_URL -> orderserviceurl."""
    if not t:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(t).lower())


def _token_variants(t):
    """
    Every spelling of a service name we might meet.

    order-svc -> {order-svc, order_svc, ordersvc, order, OrderSvc, ...}
    """
    if not t:
        return set()
    t = str(t).strip()
    out = {t, t.lower()}

    # A container image reference: registry/org/name:tag@digest. Only the
    # final path segment names the service.
    if "/" in t or ":" in t:
        tail = t.split("@")[0].split("/")[-1].split(":")[0]
        if tail and tail != t:
            out.add(tail)
            out |= _token_variants(tail)

    # split camelCase / PascalCase into words
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", t)
    words = [w for w in re.split(r"[\s._\-/:]+", spaced) if w]
    if not words:
        return out

    low = [w.lower() for w in words]
    # drop trailing environment/protocol noise: ORDER_SERVICE_URL -> order service
    while low and low[-1] in ("url", "uri", "host", "endpoint", "addr",
                              "address", "baseurl", "baseuri", "port", "name"):
        low.pop()
    if not low:
        return out

    def _emit(words):
        if not words:
            return
        out.add("-".join(words))
        out.add("_".join(words))
        out.add("".join(words))
        # naive singular/plural, so "orders" and "order" unify
        last = words[-1]
        alts = []
        if last.endswith("ies") and len(last) > 3:
            alts.append(last[:-3] + "y")
        elif last.endswith("ses") and len(last) > 3:
            alts.append(last[:-2])
        elif last.endswith("s") and not last.endswith("ss") and len(last) > 3:
            alts.append(last[:-1])
        else:
            alts.append(last + "s")
        for a in alts:
            w = words[:-1] + [a]
            out.add("-".join(w))
            out.add("_".join(w))
            out.add("".join(w))

    _emit(low)
    # and the same again with a service-ish suffix removed
    if len(low) > 1 and low[-1] in _SUFFIXES:
        _emit(low[:-1])
    return out


def find_config(start=None):
    """Walk upward looking for a config file. Returns a path or None."""
    cur = os.path.abspath(start or os.getcwd())
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        for name in DEFAULT_CONFIG_NAMES:
            cand = os.path.join(cur, name)
            if os.path.isfile(cand):
                return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def load(path=None, start=None):
    path = path or find_config(start)
    if not path:
        return Config({}, None, base_dir=start or os.getcwd())
    if not os.path.isfile(path):
        raise ConfigError("config not found: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    try:
        if path.endswith(".json"):
            data = json.loads(text)
        elif _HAVE_YAML:
            data = _yaml.safe_load(text)
        else:
            data = miniyaml.loads(text)
    except Exception as exc:
        raise ConfigError("could not parse %s: %s" % (path, exc))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("%s: top level must be a mapping" % path)
    base = os.path.dirname(os.path.abspath(path))
    # A config at config/services.yaml describes the repo one level up.
    if os.path.basename(base) == "config":
        base = os.path.dirname(base)
    return Config(data, path, base_dir=base)


def discover_repos(roots, max_depth=4, follow_symlinks=False):
    """
    Find git repositories under the given roots.

    Handles the three real layouts: sibling repos, a monorepo (one .git at the
    top), and nested repos/submodules. A directory containing .git stops the
    descent, so a repo's own vendored checkouts are not reported separately.
    """
    found = []
    seen_real = set()
    for root in roots:
        root = _expand(root)
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)

        def walk(d):
            try:
                real = os.path.realpath(d)
            except OSError:
                return
            if real in seen_real:
                return
            seen_real.add(real)
            if d.rstrip(os.sep).count(os.sep) - base_depth > max_depth:
                return
            try:
                entries = list(os.scandir(d))
            except (PermissionError, OSError):
                return
            if any(e.name == ".git" for e in entries):
                found.append(d)
                return                      # do not descend into a repo
            for e in entries:
                try:
                    if not e.is_dir(follow_symlinks=follow_symlinks):
                        continue
                except OSError:
                    continue
                if e.name in SKIP_DIRS or e.name.startswith("."):
                    continue
                walk(e.path)

        walk(root)
        if not found or all(not p.startswith(root) for p in found):
            # No git anywhere: treat immediate subdirectories as pseudo-repos
            # so a plain source drop still maps.
            try:
                subs = [e.path for e in os.scandir(root)
                        if e.is_dir() and e.name not in SKIP_DIRS
                        and not e.name.startswith(".")]
            except OSError:
                subs = []
            found.extend(subs or [root])
    # stable, de-duplicated
    out, seen = [], set()
    for p in found:
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return sorted(out)

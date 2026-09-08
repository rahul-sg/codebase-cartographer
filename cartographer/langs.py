"""
Language definitions for the regex extraction backend.

Two things make this materially better than naive line grepping:

  1. Comments and string literals are blanked before matching, so a call inside
     a doc comment or a class name inside a SQL string is not counted.
  2. Patterns are conservative. A missed definition costs recall; an invented
     one poisons the graph and, worse, gets confidently repeated back to you.

If tree_sitter is importable the AST backend in extract/symbols.py is used
instead and these patterns become the fallback.
"""
from __future__ import annotations

import re

LANG_BY_EXT = {
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".groovy": "groovy", ".gradle": "groovy",
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rb": "ruby", ".cs": "csharp", ".rs": "rust",
    ".php": "php", ".swift": "swift",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".sh": "shell", ".bash": "shell",
    ".sql": "sql", ".proto": "proto",
}

# Line-comment prefix, and block-comment delimiters, per language.
COMMENTS = {
    "java": ("//", ("/*", "*/")), "kotlin": ("//", ("/*", "*/")),
    "scala": ("//", ("/*", "*/")), "groovy": ("//", ("/*", "*/")),
    "typescript": ("//", ("/*", "*/")), "javascript": ("//", ("/*", "*/")),
    "go": ("//", ("/*", "*/")), "csharp": ("//", ("/*", "*/")),
    "rust": ("//", ("/*", "*/")), "swift": ("//", ("/*", "*/")),
    "c": ("//", ("/*", "*/")), "cpp": ("//", ("/*", "*/")),
    "php": ("//", ("/*", "*/")), "proto": ("//", ("/*", "*/")),
    "sql": ("--", ("/*", "*/")),
    "python": ("#", None), "ruby": ("#", None), "shell": ("#", None),
}

QUOTES = {
    "python": ("'", '"'), "ruby": ("'", '"'), "shell": ("'", '"'),
}
_DEFAULT_QUOTES = ("'", '"', "`")

DEFS = {
    "java": [
        ("class", re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:public|private|protected)?\s*(?:static\s+|final\s+|abstract\s+|sealed\s+|non-sealed\s+)*(class|interface|enum|record|@interface)\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:public|private|protected)\s+)?(?:static\s+|final\s+|synchronized\s+|abstract\s+|native\s+|default\s+)*(?:<[^>]+>\s+)?[\w.$<>\[\],\s?]+?\s+(\w+)\s*\([^;{]*\)\s*(?:throws\s+[\w.,\s]+)?\s*\{")),
    ],
    "python": [
        ("class", re.compile(r"^\s*class\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")),
    ],
    "typescript": [
        ("class", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?(?:class|interface|enum)\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*=")),
        ("function", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?function\s*\*?\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>")),
        ("function", re.compile(r"^\s{2,}(?:public\s+|private\s+|protected\s+|readonly\s+|static\s+|async\s+|override\s+|get\s+|set\s+)*(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[\w<>\[\]|,\s.]+)?\s*\{")),
    ],
    "go": [
        ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)")),
        ("class", re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface)")),
    ],
    "ruby": [
        ("class", re.compile(r"^\s*(?:class|module)\s+(\w+)")),
        ("function", re.compile(r"^\s*def\s+(?:self\.)?(\w+)")),
    ],
    "csharp": [
        ("class", re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:public|private|protected|internal)?\s*(?:sealed\s+|abstract\s+|static\s+|partial\s+|readonly\s+)*(?:class|interface|struct|record|enum)\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|private|protected|internal)\s+)?(?:static\s+|async\s+|virtual\s+|override\s+|sealed\s+|extern\s+|partial\s+)*[\w.<>\[\],\s?]+?\s+(\w+)\s*\([^;)]*\)\s*(?:where[^{]+)?\{")),
    ],
    "rust": [
        ("function", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+|async\s+|unsafe\s+|extern\s+\"[^\"]*\"\s*)*fn\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+(\w+)")),
    ],
    "php": [
        ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|abstract\s+|final\s+)*function\s+&?(\w+)")),
    ],
    "swift": [
        ("class", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|fileprivate\s+|final\s+)*(?:class|struct|enum|protocol|actor)\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|fileprivate\s+|static\s+|override\s+|final\s+|class\s+|mutating\s+)*func\s+(\w+)")),
    ],
    "scala": [
        ("class", re.compile(r"^\s*(?:final\s+|sealed\s+|abstract\s+|implicit\s+|case\s+)*(?:class|object|trait|enum)\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:override\s+|final\s+|private\s+|protected\s+|implicit\s+)*def\s+(\w+)")),
    ],
    "c": [
        ("function", re.compile(r"^[\w\*\s]+?\**\s*(\w+)\s*\([^;]*\)\s*\{")),
        ("class", re.compile(r"^\s*typedef\s+struct\s*(?:\w+)?\s*\{?|^\s*struct\s+(\w+)\s*\{")),
    ],
    "proto": [
        ("class", re.compile(r"^\s*(?:message|service|enum)\s+(\w+)")),
        ("function", re.compile(r"^\s*rpc\s+(\w+)")),
    ],
    "shell": [
        ("function", re.compile(r"^\s*(?:function\s+)?(\w+)\s*\(\s*\)\s*\{")),
    ],
    "sql": [
        ("function", re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+(\w+)", re.I)),
        ("class", re.compile(r"^\s*CREATE\s+(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)", re.I)),
    ],
}
DEFS["kotlin"] = [
    ("class", re.compile(r"^\s*(?:@\w+\s*)*(?:public\s+|private\s+|internal\s+|protected\s+|open\s+|abstract\s+|sealed\s+|data\s+|inner\s+|value\s+)*(?:class|interface|object|enum\s+class)\s+(\w+)")),
    ("function", re.compile(r"^\s*(?:@\w+\s*)*(?:public\s+|private\s+|internal\s+|protected\s+|open\s+|override\s+|suspend\s+|inline\s+|operator\s+)*fun\s+(?:<[^>]+>\s*)?(?:[\w.<>]+\.)?(\w+)\s*\(")),
]
DEFS["groovy"] = DEFS["java"] + [
    ("function", re.compile(r"^\s*def\s+(\w+)\s*\(")),
]
DEFS["javascript"] = DEFS["typescript"]
DEFS["cpp"] = DEFS["c"] + [
    ("class", re.compile(r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+(\w+)")),
]

# The Java class pattern captures the keyword in group 1 and name in group 2.
TWO_GROUP_DEFS = {"java", "groovy"}

IMPORTS = {
    "java":       re.compile(r"^\s*import\s+(?:static\s+)?([\w.$]+)"),
    "kotlin":     re.compile(r"^\s*import\s+([\w.$]+)"),
    "scala":      re.compile(r"^\s*import\s+([\w.$]+)"),
    "groovy":     re.compile(r"^\s*import\s+(?:static\s+)?([\w.$]+)"),
    "python":     re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))"),
    "typescript": re.compile(r"""(?:^\s*(?:import|export)\b[^'"]*?from\s+|^\s*import\s*\(?\s*|\brequire\s*\()['"]([^'"]+)['"]"""),
    "go":         re.compile(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([\w./-]+)"'),
    "ruby":       re.compile(r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]"""),
    "csharp":     re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)"),
    "rust":       re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+)"),
    "php":        re.compile(r"^\s*use\s+([\w\\]+)"),
    "swift":      re.compile(r"^\s*import\s+(\w+)"),
    "c":          re.compile(r'^\s*#include\s*[<"]([^>"]+)[>"]'),
    "proto":      re.compile(r'^\s*import\s+(?:public\s+)?"([^"]+)"'),
}
IMPORTS["javascript"] = IMPORTS["typescript"]
IMPORTS["cpp"] = IMPORTS["c"]

EXTENDS = {
    "java":       re.compile(r"\b(?:class|interface|enum|record)\s+\w+(?:\s*<[^>]*>)?(?:\s*\([^)]*\))?\s+(?:extends|implements)\s+([\w.$,<>\s]+?)(?:\{|$)"),
    "python":     re.compile(r"^\s*class\s+\w+\s*\(([^)]*)\)"),
    "typescript": re.compile(r"\b(?:class|interface)\s+\w+(?:<[^>]*>)?\s+(?:extends|implements)\s+([\w.,<>\s]+?)(?:\{|$)"),
    "csharp":     re.compile(r"\b(?:class|interface|record|struct)\s+\w+(?:<[^>]*>)?\s*:\s*([\w.,<>\s]+?)(?:\{|where|$)"),
    "ruby":       re.compile(r"^\s*class\s+\w+\s*<\s*([\w:]+)"),
    "scala":      re.compile(r"\b(?:class|object|trait)\s+\w+[^{]*?\b(?:extends|with)\s+([\w.,\[\]\s]+?)(?:\{|$)"),
    "swift":      re.compile(r"\b(?:class|struct|enum|protocol)\s+\w+\s*:\s*([\w.,<>\s]+?)(?:\{|where|$)"),
    "php":        re.compile(r"\b(?:class|interface)\s+\w+\s+(?:extends|implements)\s+([\w\\,\s]+?)(?:\{|$)"),
    "kotlin":     re.compile(r"\b(?:class|object|interface)\s+\w+[^{:]*?:\s*([\w.<>,\s()]+?)(?:\{|$)"),
}
EXTENDS["javascript"] = EXTENDS["typescript"]
EXTENDS["groovy"] = EXTENDS["java"]

CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")

# Control flow, ubiquitous stdlib, and test-framework verbs. Treating these as
# graph edges would bury the real signal under noise.
CALL_NOISE = frozenset("""
if for while switch catch return super this self new typeof sizeof delete
func def class function await yield import require export from as in is not and or
print println printf sprintf log logger debug info warn error fatal trace
assert throw raise panic recover defer go select case default break continue
String Integer Boolean Long Double Float List Map Set Optional Array Object
ArrayList HashMap HashSet LinkedList Arrays Collections Objects Math System
len str int dict list range type format join split strip lower upper sorted
enumerate zip map filter reduce sum min max abs round isinstance getattr setattr
hasattr super repr bool float tuple set frozenset bytes open close read write
test it describe expect should beforeEach afterEach before after setUp tearDown
mock when verify given then and_ assertThat assertEquals assertTrue assertNull
get set put add remove contains size empty clear run main init call apply
value key name id data result response request builder build of valueOf
toString equals hashCode clone compareTo parse valueOf now currentTimeMillis
require_once include include_once fmt make append copy cap panic
""".split())

# Language keywords that can look like a definition to a regex. Checked in
# addition to CALL_NOISE before any definition is accepted, because a keyword
# admitted as a symbol pollutes the graph and every query that touches it.
KEYWORDS = frozenset("""
synchronized volatile transient strictfp instanceof throws extends implements
package do finally else try switch native abstract sealed permits yield record
public private protected internal static final const let var readonly override
virtual partial unsafe extern inline explicit friend template typename operator
namespace struct union enum interface class def fun val lazy suspend actor
where guard defer select chan range fallthrough goto typedef sizeof alignof
elif except lambda pass global nonlocal assert raise with async await
foreach unless begin ensure rescue retry redo next then when case esac fi done
lock checked unchecked fixed stackalloc base params out ref throw catch finally
return break continue goto default null true false this super new delete
""".split())

# Cyclomatic-complexity proxy: branch keywords per line. Not a real CFG, but it
# correlates well enough with complexity to rank hotspots, which is all the
# churn-times-complexity heuristic needs.
BRANCH = re.compile(
    r"\b(if|else\s+if|elif|for|foreach|while|case|catch|except|"
    r"&&|\|\||\?\?|and\b|or\b)\b|(?<![=!<>])\?(?!\?)")


def lang_of(path):
    """Language for a path, or None if we do not parse it."""
    low = path.lower()
    for ext, lang in LANG_BY_EXT.items():
        if low.endswith(ext):
            return lang
    return None


def strip_noise(text, lang):
    """
    Blank out comments and string literals, preserving line structure and
    column offsets so reported line numbers stay exact.

    Characters are replaced with spaces rather than removed. That keeps every
    line number and column identical to the original file, which matters
    because every claim this tool makes is cited as file:line.
    """
    line_c, block = COMMENTS.get(lang, ("//", ("/*", "*/")))
    quotes = QUOTES.get(lang, _DEFAULT_QUOTES)
    out = []
    i, n = 0, len(text)
    in_block = False
    in_str = None
    line_comment = False
    bstart, bend = block if block else (None, None)

    while i < n:
        ch = text[i]
        if ch == "\n":
            out.append("\n")
            line_comment = False
            if in_str in ("'", '"'):
                in_str = None          # unterminated single-line string
            i += 1
            continue
        if in_block:
            if bend and text.startswith(bend, i):
                out.append(" " * len(bend))
                i += len(bend)
                in_block = False
            else:
                out.append(" ")
                i += 1
            continue
        if line_comment:
            out.append(" ")
            i += 1
            continue
        if in_str:
            if ch == "\\" and i + 1 < n and text[i + 1] != "\n":
                out.append("  ")
                i += 2
                continue
            if text.startswith(in_str, i):
                out.append(" " * len(in_str))
                i += len(in_str)
                in_str = None
            else:
                out.append(" ")
                i += 1
            continue
        # not in any special state
        if bstart and text.startswith(bstart, i):
            out.append(" " * len(bstart))
            i += len(bstart)
            in_block = True
            continue
        if line_c and text.startswith(line_c, i):
            out.append(" " * len(line_c))
            i += len(line_c)
            line_comment = True
            continue
        for q in ('"""', "'''"):
            if lang == "python" and text.startswith(q, i):
                in_str = q
                out.append(" " * 3)
                i += 3
                break
        else:
            if ch in quotes:
                in_str = ch
                out.append(" ")
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
    return "".join(out)


def complexity(text, lang):
    """Branch-keyword count. A proxy, and labelled as one everywhere it is used."""
    return len(BRANCH.findall(text))

"""A strict `.env` parser, compatible with the python-dotenv / dotenv file format.

    # comment
    export API_URL=https://api.example.com   # `export` is optional, a ` #` starts a comment
    GREETING="Hello\\nworld"                   # double quotes: \\n \\t \\r \\" \\\\ \\$ escapes, ${VAR} expansion
    RAW='no ${expansion} here'                # single quotes: literal (only \\' and \\\\ are escapes)
    PEM="-----BEGIN CERTIFICATE-----
    MIIB...
    -----END CERTIFICATE-----"                # quoted values may span lines
    EMPTY=                                    # an empty string
    URL=${API_URL}/v1                         # ${VAR}, ${VAR:-default} (unset or empty), ${VAR-default} (unset)

Mistakes are errors with the file and line, never silently guessed values. `$${` writes a literal `${`.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REF_BODY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:(:?-)(.*))?$", re.S)
DOUBLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "$": "$", "'": "'"}


class DotenvError(ValueError):
    """A `.env` file that does not parse, or a `${VAR}` that cannot be resolved."""


class Entry:
    """One `KEY=value` line. `raw` is the value with quotes and escapes handled; `expand` is False for
    single-quoted values, whose `${...}` stays literal."""

    __slots__ = ("key", "raw", "expand", "line", "path")

    def __init__(self, key: str, raw: str, expand: bool, line: int, path: str) -> None:
        self.key, self.raw, self.expand, self.line, self.path = key, raw, expand, line, path

    @property
    def where(self) -> str:
        return "%s:%d" % (self.path, self.line)

    def __repr__(self) -> str:
        return "Entry(%s, %s)" % (self.key, self.where)


def parse(text: str, path: str = ".env") -> List[Entry]:
    """The entries of a `.env` file, in file order (a key given twice appears twice; the last one wins)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: List[Entry] = []
    i, n, line = 0, len(text), 1

    def fail(msg: str, at: int) -> DotenvError:
        return DotenvError("%s:%d: %s" % (path, at, msg))

    while i < n:
        end = text.find("\n", i)
        end = n if end < 0 else end
        stripped = text[i:end].strip()
        if not stripped or stripped.startswith("#"):
            i, line = end + 1, line + 1
            continue
        start_line = line
        j = i + (len(text[i:end]) - len(text[i:end].lstrip()))
        if text.startswith("export", j) and j + 6 < end and text[j + 6] in " \t":
            j = j + 6
            while j < end and text[j] in " \t":
                j += 1
        m = KEY_RE.match(text, j)
        if not m:
            raise fail("expected KEY=value (keys are letters, digits and underscores, not starting with a digit)", line)
        key, j = m.group(0), m.end()
        while j < end and text[j] in " \t":
            j += 1
        if j >= end or text[j] != "=":
            raise fail("expected '=' after %s" % key, line)
        j += 1
        while j < end and text[j] in " \t":
            j += 1
        if j < n and text[j] in "'\"":
            q = text[j]
            buf: List[str] = []
            k = j + 1
            while True:
                if k >= n:
                    raise fail("%s: the %s quote opened here is never closed" % (key, "double" if q == '"' else "single"),
                               start_line)
                c = text[k]
                if c == "\\" and k + 1 < n:
                    nxt = text[k + 1]
                    if q == '"' and nxt in DOUBLE_ESCAPES:
                        # `\$` is a plain `$`; before `{` it becomes `$${`, which expand() turns into a literal `${`.
                        buf.append("$$" if nxt == "$" and text.startswith("{", k + 2) else
                                   "$" if nxt == "$" else DOUBLE_ESCAPES[nxt])
                        k += 2
                        continue
                    if q == "'" and nxt in "'\\":
                        buf.append(nxt)
                        k += 2
                        continue
                if c == q:
                    break
                if c == "\n":
                    line += 1
                buf.append(c)
                k += 1
            end = text.find("\n", k)
            end = n if end < 0 else end
            rest = text[k + 1:end].strip()
            if rest and not rest.startswith("#"):
                raise fail("%s: unexpected text after the closing quote: %r" % (key, rest), line)
            raw, expand = "".join(buf), q == '"'
        else:
            # A `#` starts a comment only after whitespace, so `COLOR=#ff0000` keeps its value.
            value = text[j:end]
            cut = re.search(r"\s#", value)
            if cut:
                value = value[:cut.start()]
            elif value.startswith("#") and text[j - 1] in " \t":
                value = ""
            raw, expand = value.strip(), True
        out.append(Entry(key, raw, expand, start_line, path))
        i, line = end + 1, line + 1
    return out


def _close(value: str, start: int) -> int:
    """Index of the `}` that closes the `${` before `start`, or -1. Nested `${...}` in a default are skipped."""
    depth, i = 1, start
    while i < len(value):
        if value.startswith("$${", i):
            i += 3
        elif value.startswith("${", i):
            depth, i = depth + 1, i + 2
        elif value[i] == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
    return -1


def expand(value: str, lookup: Callable[[str], Optional[str]], where: str = "") -> str:
    """Replaces `${VAR}`, `${VAR:-default}` and `${VAR-default}` in `value`; `$${` gives a literal `${`.
    A default may hold references itself: `${API_URL:-https://${HOST}}`.
    `lookup(name)` returns the (already expanded) value or None when the variable is not set. A plain `${VAR}`
    that is not set is an error, not an empty string: write `${VAR:-}` to allow empty."""
    at = where + ": " if where else ""
    out: List[str] = []
    i = 0
    while True:
        j = value.find("$", i)
        if j < 0:
            out.append(value[i:])
            return "".join(out)
        out.append(value[i:j])
        if value.startswith("$${", j):
            out.append("${")
            i = j + 3
            continue
        if not value.startswith("${", j):
            out.append("$")
            i = j + 1
            continue
        k = _close(value, j + 2)
        if k < 0:
            raise DotenvError("%s%r has a ${ that is never closed with } (write $${ for a literal ${, or "
                              "single-quote the value)"
                              % (at, value[j:j + 40]))
        body = REF_BODY_RE.match(value[j + 2:k])
        if not body:
            raise DotenvError("%s%r is not a valid reference (use ${NAME} or ${NAME:-default})"
                              % (at, value[j:k + 1]))
        name, op, default = body.group(1), body.group(2), body.group(3)
        v = lookup(name)
        if (op == ":-" and not v) or (op == "-" and v is None):
            v = expand(default or "", lookup, where)
        elif v is None:
            raise DotenvError("%s${%s} is not set (define it, or write ${%s:-} to allow an empty value)"
                              % (at, name, name))
        out.append(v)
        i = k + 1

def resolve(entries: Dict[str, Entry], environ: Dict[str, str]) -> Dict[str, str]:
    """Expanded values of `entries` ({key: winning entry}). A reference resolves to the environment first (it wins
    over files), then to another entry, whatever file that entry came from. Reference cycles are errors."""
    done: Dict[str, str] = {}
    active: List[str] = []

    def lookup(name: str) -> Optional[str]:
        if name in environ:
            return environ[name]
        if name not in entries:
            return None
        return value(name)

    def value(key: str) -> str:
        if key in done:
            return done[key]
        e = entries[key]
        if key in active:
            raise DotenvError("%s: %s refers back to itself (%s)" % (e.where, key, " -> ".join(active + [key])))
        active.append(key)
        try:
            v = expand(e.raw, lookup, "%s: %s" % (e.where, key)) if e.expand else e.raw
        finally:
            active.pop()
        done[key] = v
        return v

    return {k: value(k) for k in entries}


def dumps_value(value: str) -> str:
    """`value` written so that `parse` reads it back unchanged (for generated files)."""
    if value == "" or re.match(r"^[A-Za-z0-9_./:@+,=-]+$", value):
        return value
    if "'" not in value and "\n" not in value and "\\" not in value:
        return "'%s'" % value
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("$", "\\$")


def pairs(entries: List[Entry]) -> List[Tuple[str, str]]:
    return [(e.key, e.raw) for e in entries]

"""The optional variable schema in `[tool.pyxpo.module_options.dot-env.vars]`:

    PYXPO_PUBLIC_API_URL = { type = "url", required = true, description = "Backend base URL" }
    PYXPO_PUBLIC_RETRIES = { type = "int", default = 3 }
    PYXPO_PUBLIC_THEME   = { choices = ["light", "dark"], default = "light" }
    PYXPO_PUBLIC_FLAGS   = "list"                      # short form: just the type
    STRIPE_SECRET_KEY    = { required = true }         # build-only: checked, never shipped
    PYXPO_PUBLIC_ADMOB_APP_ID = { required = ["production"] }   # required only in these build modes

Types: str (default), int, float, bool (true/false, 1/0, yes/no, on/off), url, list (comma separated), json.
Declared public variables ship as typed JSON; undeclared ones ship as strings.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

TYPES = ("str", "int", "float", "bool", "url", "list", "json")
FIELDS = ("type", "required", "default", "description", "choices", "pattern", "example")
TRUE, FALSE = ("true", "1", "yes", "on"), ("false", "0", "no", "off")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Var:
    __slots__ = ("name", "type", "required", "default", "description", "choices", "pattern", "example")

    def __init__(self, name: str, type: str = "str", required: Union[bool, List[str]] = False, default: Any = None,
                 description: str = "", choices: Optional[List[Any]] = None, pattern: Optional[str] = None,
                 example: Optional[str] = None) -> None:
        self.name, self.type, self.required, self.default = name, type, required, default
        self.description, self.choices, self.pattern, self.example = description, choices, pattern, example

    def required_in(self, mode: str) -> bool:
        """`required = true` holds in every mode; `required = ["production"]` only in the modes listed."""
        return self.required if isinstance(self.required, bool) else mode in self.required


def parse_schema(table: Any) -> Tuple[Dict[str, Var], List[str]]:
    """{name: Var} from the `vars` table, and the mistakes in it."""
    out: Dict[str, Var] = {}
    errors: List[str] = []
    if table is None:
        return out, errors
    if not isinstance(table, dict):
        return out, ["module_options.dot-env.vars must be a table of NAME = { type = ... }"]
    for name, spec in table.items():
        where = "module_options.dot-env.vars.%s" % name
        if not NAME_RE.match(name):
            errors.append("%s: %r is not a valid variable name" % (where, name))
            continue
        if isinstance(spec, str):
            spec = {"type": spec}
        if not isinstance(spec, dict):
            errors.append("%s must be a type name or a table" % where)
            continue
        unknown = sorted(set(spec) - set(FIELDS))
        if unknown:
            errors.append("%s: unknown field %s (known: %s)" % (where, ", ".join(unknown), ", ".join(FIELDS)))
            continue
        t = spec.get("type", "str")
        if t not in TYPES:
            errors.append("%s: type %r is not one of %s" % (where, t, ", ".join(TYPES)))
            continue
        required = spec.get("required", False)
        if not isinstance(required, bool) and not (isinstance(required, list) and required and all(
                isinstance(m, str) and m for m in required)):
            errors.append("%s: required must be true, false or a list of build modes like [\"production\"]" % where)
            continue
        choices = spec.get("choices")
        if choices is not None and (not isinstance(choices, list) or not choices):
            errors.append("%s: choices must be a non-empty list" % where)
            continue
        pattern = spec.get("pattern")
        if pattern is not None:
            try:
                re.compile(pattern)
            except (re.error, TypeError) as e:
                errors.append("%s: pattern is not a valid regular expression (%s)" % (where, e))
                continue
        var = Var(name, t, spec.get("required", False), spec.get("default"), str(spec.get("description", "")),
                  choices, pattern, None if spec.get("example") is None else str(spec["example"]))
        if var.default is not None:
            try:
                var.default = check(var, var.default)
            except ValueError as e:
                errors.append("%s: default %s" % (where, e))
                continue
        out[name] = var
    return out, errors


def coerce(type: str, value: Any) -> Any:
    """`value` (usually the string from a `.env` file) as `type`. Raises ValueError with a short reason."""
    if type == "str":
        if isinstance(value, (dict, list)):
            raise ValueError("must be text")
        return value if isinstance(value, str) else json.dumps(value) if isinstance(value, bool) else str(value)
    if type == "int":
        if isinstance(value, bool):
            raise ValueError("must be a whole number")
        if isinstance(value, int):
            return value
        s = str(value).strip().replace("_", "")
        if not re.match(r"^[+-]?\d+$", s):
            raise ValueError("must be a whole number, got %r" % value)
        return int(s)
    if type == "float":
        if isinstance(value, bool):
            raise ValueError("must be a number")
        if isinstance(value, (int, float)):
            return float(value)
        try:
            f = float(str(value).strip())
        except ValueError:
            raise ValueError("must be a number, got %r" % value)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number, got %r" % value)
        return f
    if type == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in TRUE:
            return True
        if s in FALSE:
            return False
        raise ValueError("must be true/false (or 1/0, yes/no, on/off), got %r" % value)
    if type == "url":
        s = str(value).strip()
        try:
            u = urlsplit(s)
        except ValueError:
            u = None
        if not u or not u.scheme or not u.netloc or not re.match(r"^[a-z][a-z0-9+.-]*$", u.scheme, re.I):
            raise ValueError("must be a full URL such as https://api.example.com, got %r" % value)
        return s
    if type == "list":
        if isinstance(value, list):
            return [str(x) for x in value]
        return [p.strip() for p in str(value).split(",") if p.strip()]
    if type == "json":
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except ValueError as e:
            raise ValueError("must be JSON (%s)" % e)
    raise ValueError("unknown type %r" % type)


def check(var: Var, value: Any) -> Any:
    """`value` coerced to `var.type` and checked against its choices and pattern."""
    v = coerce(var.type, value)
    if var.pattern is not None and not re.fullmatch(var.pattern, value if isinstance(value, str) else str(v)):
        raise ValueError("does not match the pattern %s" % var.pattern)
    if var.choices is not None:
        allowed = [coerce(var.type, c) if not isinstance(c, (dict, list)) else c for c in var.choices]
        if v not in allowed:
            raise ValueError("must be one of %s, got %r" % (", ".join(str(c) for c in var.choices), value))
    return v


def insecure_url(value: str) -> bool:
    """True for a plain-http URL to another machine (fine for local development, not for a release)."""
    try:
        u = urlsplit(value)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1", "10.0.2.2") and \
        not host.endswith(".localhost") and not host.endswith(".local")

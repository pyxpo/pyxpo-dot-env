"""The pyxpo build hook of pyxpo-dot-env (runs on the build machine, never inside the app).

Each time pyxpo loads `[tool.pyxpo]` it

1. reads `.env`, `.env.<mode>`, `.env.local`, `.env.<mode>.local` (the environment wins) and puts the values
   into `os.environ` for `app_config.py` and build tools,
2. replaces `${VAR}` in `[tool.pyxpo]` strings (only `PYXPO_PUBLIC_` variables may go into `extra`, which ships),
3. checks the schema in `module_options.dot-env.vars`, the leak guard and git hygiene,
4. ships the `PYXPO_PUBLIC_` variables into the app, read with `from pyxpo_dot_env import env`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import PREFIX, guard, schema
from .dotenv import DotenvError, expand
from .loader import Loaded, environment_values, inject, load

NAME = "dot-env"
OPTION_KEYS = ("vars", "strict", "allow", "dir", "git_check")


class Report:
    """What `analyze` found. `public` is {name: typed value} that would ship; `errors` stop the build."""

    def __init__(self, loaded: Loaded) -> None:
        self.loaded = loaded
        self.public: Dict[str, Any] = {}
        self.schema: Dict[str, schema.Var] = {}
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.git_check = True

    @property
    def mode(self) -> str:
        return self.loaded.mode


def options_of(config: Mapping[str, Any]) -> Dict[str, Any]:
    opts = config.get("module_options", config.get("moduleOptions")) or {}
    mine = opts.get(NAME) if isinstance(opts, dict) else None
    return mine if isinstance(mine, dict) else {}


def env_dir(project: Path, options: Mapping[str, Any]) -> Path:
    d = options.get("dir")
    if d is None:
        return Path(project)
    if not isinstance(d, str) or not d.strip():
        raise DotenvError("module_options.dot-env.dir must be a folder path")
    return (Path(project) / d).resolve()


def analyze(project: Path, mode: str, config: Mapping[str, Any],
            environ: Optional[Mapping[str, str]] = None) -> Report:
    """Loads the env files for `mode` and runs every check. Shared by the build hook and `python -m pyxpo_dot_env`."""
    options = options_of(config)
    loaded = load(env_dir(project, options), mode, environ)
    report = Report(loaded)
    errors, warnings = report.errors, report.warnings

    unknown = sorted(set(options) - set(OPTION_KEYS))
    if unknown:
        errors.append("module_options.dot-env: unknown key %s (known: %s)" % (", ".join(unknown), ", ".join(OPTION_KEYS)))
    allow = options.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        errors.append("module_options.dot-env.allow must be a list of variable names")
        allow = []
    strict = options.get("strict", False)
    if not isinstance(strict, bool):
        errors.append("module_options.dot-env.strict must be true or false")
        strict = False
    git_check = options.get("git_check", True)
    if not isinstance(git_check, bool):
        errors.append("module_options.dot-env.git_check must be true or false")
        git_check = True
    report.git_check = git_check
    report.schema, schema_errors = schema.parse_schema(options.get("vars"))
    errors.extend(schema_errors)

    values = dict(loaded.values)
    values.update(environment_values(PREFIX, environ))
    locked = sorted(name for name, value in values.items() if guard.encrypted(value))
    if locked:
        errors.append("%s %s encrypted (dotenvx); decrypt for the build with: dotenvx run -- pyxpo build ..."
                      % (", ".join(locked), "is" if len(locked) == 1 else "are"))
        return report

    for name in sorted(loaded.entries):
        p = guard.other_prefix(name)
        if p:
            warnings.append("%s uses the %s prefix of another framework; only %s variables reach a pyxpo app"
                            % (name, p, PREFIX))

    for name, var in sorted(report.schema.items()):
        required = var.required_in(mode)
        if name not in values or (values[name] == "" and required):
            if required:
                errors.append("%s is required%s but not set%s%s" % (
                    name, "" if var.required is True else " in %s builds" % mode,
                    " (%s)" % var.description if var.description else "",
                    "; for example %s=%s" % (name, var.example) if var.example else ""))
            elif var.default is not None and name.startswith(PREFIX):
                report.public[name] = var.default
            continue
        try:
            typed = schema.check(var, values[name])
        except ValueError as e:
            errors.append("%s %s (from %s)" % (name, e, loaded.sources.get(name, "the environment")))
            continue
        if name.startswith(PREFIX):
            report.public[name] = typed

    for name, value in sorted(values.items()):
        if not name.startswith(PREFIX) or name in report.public or name in report.schema:
            continue
        if strict:
            errors.append("%s is not declared in module_options.dot-env.vars (strict = true)" % name)
            continue
        report.public[name] = value

    for name, value in sorted(report.public.items()):
        text = value if isinstance(value, str) else str(value)
        leak = guard.check_public(name, text, allow)
        if leak:
            errors.append(leak)
        if mode == "production" and isinstance(value, str) and schema.insecure_url(value):
            warnings.append("%s is a plain http:// URL in a production build (%s); use https://" % (name, value))
    return report


def interpolate(config: Dict[str, Any], values: Mapping[str, str]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """`config` with `${VAR}` replaced in every string (but not inside module_options.dot-env). Returns the new table,
    the errors, and the names used in each place for the leak checks."""
    errors: List[str] = []
    notes: List[str] = []

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            return {k: node[k] if _skip(path, k) else walk(node[k], "%s.%s" % (path, k) if path else k)
                    for k in node}
        if isinstance(node, list):
            return [walk(v, "%s[%d]" % (path, i)) for i, v in enumerate(node)]
        if isinstance(node, str) and "${" in node:
            used: List[str] = []

            def lookup(name: str) -> Optional[str]:
                used.append(name)
                return values.get(name)
            try:
                out = expand(node, lookup, "[tool.pyxpo] %s" % path)
            except DotenvError as e:
                errors.append(str(e))
                return node
            ships = path == "extra" or path.startswith("extra.") or path.startswith("extra[")
            for name in used:
                if ships and not name.startswith(PREFIX):
                    errors.append("[tool.pyxpo] %s uses ${%s}, but extra ships inside the app; only %s "
                                  "variables may go there" % (path, name, PREFIX))
                elif guard.secret_kind(values.get(name) or ""):
                    notes.append("[tool.pyxpo] %s uses ${%s}, which looks like a secret; make sure that setting "
                                 "does not end up inside the app" % (path, name))
            return out
        return node

    return walk(config, ""), errors, notes


def _skip(path: str, key: str) -> bool:
    return path in ("module_options", "moduleOptions") and key == NAME


def git_warnings(project: Path, loaded: Loaded) -> List[str]:
    """Hints about env files that git would commit. Silent when git or the repository is not there."""
    out: List[str] = []
    try:
        root = subprocess.run(["git", "-C", str(project), "rev-parse", "--show-toplevel"], capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return out
    if root.returncode != 0:
        return out
    d = loaded.directory
    names = [p.name for p in loaded.files] + [".env.local", ".env.%s.local" % loaded.mode, ".env.keys"]
    for name in dict.fromkeys(names):
        if not (name.endswith(".local") or name == ".env.keys"):
            continue
        try:
            r = subprocess.run(["git", "-C", str(d), "check-ignore", "-q", "--no-index", name], capture_output=True,
                               timeout=5)
        except (OSError, subprocess.SubprocessError):
            return out
        if r.returncode == 1:
            out.append("%s is not in .gitignore; add .env*.local and .env.keys so machine secrets are never "
                       "committed (python -m pyxpo_dot_env init does it)" % name)
    try:
        tracked = subprocess.run(["git", "-C", str(d), "ls-files", "--", ".env", ".env.%s" % loaded.mode],
                                 capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        tracked = []
    for name in tracked:
        secret = [e.key for e in loaded.entries.values() if e.path == name and not e.key.startswith(PREFIX)
                  and (guard.secret_kind(e.raw) or guard.name_hint(e.key))]
        if secret:
            out.append("%s is committed to git and holds %s; move secrets to .env.local or the CI environment"
                       % (name, ", ".join(sorted(secret))))
    return out


def stray_env_files(project: Path) -> List[str]:
    """`.env` files inside app/ or assets/, which would be bundled on platforms that copy the folder as-is."""
    out = []
    for sub in ("app", "assets"):
        base = Path(project) / sub
        if base.is_dir():
            for p in sorted(base.rglob(".env*")):
                if p.is_file():
                    out.append(str(p.relative_to(project)))
    return out


def configure(ctx: Any) -> Optional[Dict[str, Any]]:
    project = Path(ctx.project)
    try:
        report = analyze(project, ctx.mode, ctx.config)
    except DotenvError as e:
        ctx.fail(str(e))
        return None
    loaded = report.loaded
    inject(loaded)
    values: Dict[str, str] = dict(os.environ)
    values.update(loaded.values)
    config, interp_errors, notes = interpolate(dict(ctx.config), values)
    errors = report.errors + interp_errors
    if errors:
        files = ", ".join(p.name for p in loaded.files) or "no .env files"
        ctx.fail("%d problem%s with the environment for mode %r (%s in %s):\n  - %s" % (
            len(errors), "" if len(errors) == 1 else "s", ctx.mode, files, loaded.directory,
            "\n  - ".join(errors)))
    for w in report.warnings + notes + (git_warnings(project, loaded) if report.git_check else []):
        ctx.warn(w)
    for path in stray_env_files(project):
        ctx.warn("%s is inside a bundled folder; keep .env files in the project root" % path)
    ctx.ship({"mode": ctx.mode, "vars": report.public})
    return config


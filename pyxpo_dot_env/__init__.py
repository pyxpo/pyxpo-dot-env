"""pyxpo-dot-env: `.env` files for pyxpo apps, with typed access and a build-time leak guard.

    # .env
    PYXPO_PUBLIC_API_URL=https://api.example.com     # PYXPO_PUBLIC_ variables ship inside the app
    SENTRY_AUTH_TOKEN=...                            # everything else stays on the build machine

    from pyxpo_dot_env import env

    env.url("API_URL")                    # "https://api.example.com" (the PYXPO_PUBLIC_ prefix is optional)
    env.int("RETRIES", 3)                 # typed getters with defaults: str, int, float, bool, url, list, json
    env.require("API_URL", "SUPPORT_EMAIL")
    env.mode, env.is_production

Only `PYXPO_PUBLIC_` variables reach the app, and anything that ships can be read by whoever has the app.
Keep server secrets on your server; see the README for the backend-proxy pattern.
"""
from __future__ import annotations

import contextlib
import copy
import json
from typing import Any, Dict, Iterator, List, Optional

__version__ = "0.1.0a1"
PREFIX = "PYXPO_PUBLIC_"
_MISSING = object()


class EnvError(LookupError):
    """A variable that is missing, or that does not have the type asked for."""


def _data() -> Dict[str, Any]:
    try:
        from pyxpo.module_api import build_data
    except ImportError:  # outside a pyxpo app (plain scripts, tests)
        return {}
    data = build_data("dot-env")
    return data if isinstance(data, dict) else {}


def _coerce(type: str, value: Any) -> Any:
    from .schema import coerce
    return coerce(type, value)


class Env:
    """The `PYXPO_PUBLIC_` variables the build shipped. Names may be given with or without the prefix."""

    def __init__(self) -> None:
        self._loaded: Optional[Dict[str, Any]] = None
        self._overrides: List[Dict[str, Any]] = []

    def _all(self) -> Dict[str, Any]:
        if self._loaded is None:
            self._loaded = _data()
        out = dict(self._loaded.get("vars") or {})
        for layer in self._overrides:
            for k, v in layer.items():
                if v is None:
                    out.pop(k, None)
                else:
                    out[k] = v
        return out

    def _key(self, name: str) -> str:
        if not isinstance(name, str) or not name:
            raise TypeError("variable names are non-empty strings")
        return name if name.startswith(PREFIX) else PREFIX + name

    def _missing(self, key: str) -> EnvError:
        return EnvError("%s is not set. Add it to .env (or the build environment) and rebuild. Only %s "
                        "variables ship inside the app; build-only variables never do." % (key, PREFIX))

    def _get(self, name: str, type: str, default: Any) -> Any:
        key = self._key(name)
        values = self._all()
        if key not in values:
            if default is _MISSING:
                raise self._missing(key)
            return default
        try:
            return _coerce(type, values[key])
        except ValueError as e:
            raise EnvError("%s %s" % (key, e))

    # typed getters ---------------------------------------------------------------------------------------------

    def str(self, name: str, default: Any = _MISSING) -> Any:
        return self._get(name, "str", default)

    def int(self, name: str, default: Any = _MISSING) -> Any:
        return self._get(name, "int", default)

    def float(self, name: str, default: Any = _MISSING) -> Any:
        return self._get(name, "float", default)

    def bool(self, name: str, default: Any = _MISSING) -> Any:
        return self._get(name, "bool", default)

    def url(self, name: str, default: Any = _MISSING) -> Any:
        return self._get(name, "url", default)

    def list(self, name: str, default: Any = _MISSING) -> Any:
        return self._get(name, "list", default)

    def json(self, name: str, default: Any = _MISSING) -> Any:
        return self._get(name, "json", default)

    # mapping-style access ---------------------------------------------------------------------------------------

    def get(self, name: str, default: Any = None) -> Any:
        """The value as the build shipped it (typed when declared in the schema, else a string)."""
        return copy.deepcopy(self._all().get(self._key(name), default))

    def require(self, *names: str) -> None:
        """Raises one EnvError that lists every name in `names` that is not set."""
        values = self._all()
        missing = [self._key(n) for n in names if self._key(n) not in values]
        if missing:
            raise EnvError("missing %s. Add %s to .env (or the build environment) and rebuild."
                           % (", ".join(missing), "it" if len(missing) == 1 else "them"))

    def __getitem__(self, name: str) -> Any:
        values = self._all()
        key = self._key(name)
        if key not in values:
            raise self._missing(key)
        return copy.deepcopy(values[key])

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and bool(name) and self._key(name) in self._all()

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._all()))

    def __len__(self) -> int:
        return len(self._all())

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._all())

    @property
    def mode(self) -> str:
        """The build mode: development, production, or what `--mode` / PYXPO_MODE set (staging, ...)."""
        if self._loaded is None:
            self._loaded = _data()
        return self._loaded.get("mode") or "development"

    @property
    def is_production(self) -> bool:
        return self.mode == "production"

    @property
    def is_development(self) -> bool:
        return self.mode == "development"

    @contextlib.contextmanager
    def override(self, **values: Any) -> Iterator["Env"]:
        """For tests: `with env.override(API_URL="http://localhost:8000"):` (None removes a variable)."""
        self._overrides.append({self._key(k): v for k, v in values.items()})
        try:
            yield self
        finally:
            self._overrides.pop()

    def reload(self) -> None:
        """Reads the shipped data again (after a hot reload)."""
        self._loaded = None

    def __repr__(self) -> str:
        return "<pyxpo_dot_env.env mode=%s %s>" % (self.mode, json.dumps(sorted(self._all())))


env = Env()

__all__ = ["env", "Env", "EnvError", "PREFIX", "__version__"]

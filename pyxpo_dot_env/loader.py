"""Finds and merges the `.env` files of a project for one build mode.

Files, lowest to highest priority (the same order as Expo, Vite and Next.js):

    .env                  shared defaults, may be committed (no secrets)
    .env.<mode>           per mode (.env.development, .env.production, .env.staging), may be committed
    .env.local            this machine only, git-ignored
    .env.<mode>.local     this machine and mode only, git-ignored

Variables already set in the process environment win over every file, so CI can inject real values without a
file on disk. `PYXPO_NO_DOTENV=1` skips the files completely (only the environment is used).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Mapping, MutableMapping, Optional, Set

from .dotenv import DotenvError, Entry, parse, resolve

SKIP_ENV = "PYXPO_NO_DOTENV"
ENVIRONMENT = "environment"
# Keys this process put into os.environ from files. A later load (the web dev server rebuilds on every change)
# must not mistake them for real environment variables, or an edited .env would never take effect.
_injected: Dict[str, str] = {}


def file_names(mode: str) -> List[str]:
    """The `.env` file names for `mode`, lowest priority first."""
    return [".env", ".env.%s" % mode, ".env.local", ".env.%s.local" % mode]


class Loaded:
    """The result of `load`: `values` {name: value}, `sources` {name: file name or "environment"}, the `files`
    that exist (lowest priority first) and the `entries` {name: winning Entry} that came from files."""

    def __init__(self, mode: str, directory: Path, values: Dict[str, str], sources: Dict[str, str],
                 files: List[Path], entries: Dict[str, Entry]) -> None:
        self.mode, self.directory, self.values, self.sources = mode, directory, values, sources
        self.files, self.entries = files, entries

    def from_file(self, name: str) -> bool:
        return self.sources.get(name, ENVIRONMENT) != ENVIRONMENT


def real_environ(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The process environment without the values a previous `inject` put there."""
    env = dict(os.environ if environ is None else environ)
    for k, v in _injected.items():
        if env.get(k) == v:
            del env[k]
    return env


def load(directory: Path, mode: str, environ: Optional[Mapping[str, str]] = None) -> Loaded:
    """Reads the `.env` files of `directory` for `mode` and merges them with the environment (which wins)."""
    directory = Path(directory)
    env = real_environ(environ)
    files: List[Path] = []
    entries: Dict[str, Entry] = {}
    if env.get(SKIP_ENV, "").strip().lower() not in ("1", "true", "yes"):
        if mode == "local":
            raise DotenvError("the build mode cannot be 'local' (.env.local is the machine-only file of every mode)")
        for name in file_names(mode):
            path = directory / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise DotenvError("%s is not UTF-8 text" % name)
            files.append(path)
            for e in parse(text, name):
                entries[e.key] = e
    file_entries = {k: e for k, e in entries.items() if k not in env}
    values = resolve(file_entries, env)
    sources = {k: e.path for k, e in file_entries.items()}
    for k in entries:
        if k in env:
            values[k], sources[k] = env[k], ENVIRONMENT
    return Loaded(mode, directory, values, sources, files, entries)


def environment_values(prefix: str, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Real environment variables whose name starts with `prefix` (they ship even with no `.env` file)."""
    return {k: v for k, v in real_environ(environ).items() if k.startswith(prefix)}


def inject(loaded: Loaded, environ: Optional[MutableMapping[str, str]] = None) -> Set[str]:
    """Puts the file values of `loaded` into `environ` (os.environ by default) so that build tools and
    `app_config.py` see them through `os.environ`. Never overrides a real environment variable. Values from a
    previous `inject` are replaced, and keys removed from the files are removed again. Returns the keys set."""
    target = os.environ if environ is None else environ
    for k, v in list(_injected.items()):
        if target.get(k) == v:
            del target[k]
        del _injected[k]
    done: Set[str] = set()
    for k, v in loaded.values.items():
        if loaded.from_file(k) and k not in target:
            target[k] = v
            _injected[k] = v
            done.add(k)
    return done

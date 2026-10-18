"""The leak guard: stops a build that would ship something that looks like a server secret.

Everything a `PYXPO_PUBLIC_` variable holds ends up inside the app package, where anyone can read it with an
unzip tool. The guard checks the public values for well-known secret formats (cloud keys, payment secret keys,
private keys, service-role tokens, passwords in URLs) and the public names for words that mean "secret".

Keys made to be public are not flagged: Google/Firebase browser keys (AIza...), Stripe publishable keys
(pk_...), Supabase anon keys, Sentry DSNs, OAuth client IDs. Restrict those in their provider's console.
A false alarm is silenced per name with `allow = ["PYXPO_PUBLIC_NAME"]`.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Iterable, List, Optional, Tuple

PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Stripe secret or restricted key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{10,}")),
    ("an Anthropic API key", re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}")),
    ("an OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[0-9A-Za-z_-]{20,}")),
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{40,})")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}")),
    ("a Google OAuth client secret", re.compile(r"\bGOCSPX-[0-9A-Za-z_-]{20,}")),
    ("a SendGrid API key", re.compile(r"\bSG\.[0-9A-Za-z_-]{16,}\.[0-9A-Za-z_-]{16,}")),
    ("a Twilio auth token or API key", re.compile(r"\bSK[0-9a-f]{32}\b")),
    ("a Google service account key", re.compile(r'"type"\s*:\s*"service_account"')),
    ("a password inside a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]+@", re.I)),
]
NAME_WORDS = ("SECRET", "PASSWORD", "PASSWD", "PRIVATE", "SERVICE_ROLE", "CREDENTIAL")
JWT_RE = re.compile(r"\beyJ[0-9A-Za-z_-]+\.(eyJ[0-9A-Za-z_-]+)\.[0-9A-Za-z_-]+")
OTHER_PREFIXES = ("EXPO_PUBLIC_", "VITE_", "NEXT_PUBLIC_", "REACT_APP_")
ENCRYPTED = "encrypted:"


def _jwt_role(value: str) -> Optional[str]:
    for m in JWT_RE.finditer(value):
        part = m.group(1)
        try:
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (ValueError, TypeError):
            continue
        if isinstance(claims, dict) and isinstance(claims.get("role"), str):
            return claims["role"]
    return None


def secret_kind(value: str) -> Optional[str]:
    """What kind of secret `value` looks like, or None."""
    if not isinstance(value, str) or len(value) < 8:
        return None
    for kind, pattern in PATTERNS:
        if pattern.search(value):
            return kind
    if _jwt_role(value) == "service_role":
        return "a Supabase service_role key (use the anon key in apps)"
    return None


def name_hint(name: str) -> Optional[str]:
    """The secret-sounding word in `name`, or None."""
    upper = name.upper()
    for word in NAME_WORDS:
        if word in upper:
            return word
    return None


def mask(value: str) -> str:
    """`value` with all but a short prefix hidden, for logs."""
    if len(value) < 12:
        return "*" * len(value)
    return value[:min(4, len(value) // 4)].replace("\n", " ") + "*" * 12 + " (%d chars)" % len(value)


def check_public(name: str, value: str, allow: Iterable[str] = ()) -> Optional[str]:
    """Why shipping `name=value` inside the app is a leak, or None."""
    if name in allow:
        return None
    kind = secret_kind(value)
    if kind:
        return "%s looks like %s (%s). It would ship inside the app, where anyone can extract it. Keep it " \
               "build-only (drop the PYXPO_PUBLIC_ prefix) and call the API from your server, or add %s to " \
               "module_options.dot-env.allow if this value is meant to be public." % (name, kind, mask(value), name)
    word = name_hint(name)
    if word:
        return "%s has %s in its name but would ship inside the app, where anyone can extract it. Rename it " \
               "without the PYXPO_PUBLIC_ prefix to keep it build-only, or add it to module_options.dot-env.allow " \
               "if it is not a secret." % (name, word)
    return None


def encrypted(value: str) -> bool:
    """True for a dotenvx-encrypted value, which must be decrypted before the build reads it."""
    return isinstance(value, str) and value.startswith(ENCRYPTED)


def other_prefix(name: str) -> Optional[str]:
    for p in OTHER_PREFIXES:
        if name.startswith(p):
            return p
    return None

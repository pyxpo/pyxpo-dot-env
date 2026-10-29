"""pyxpo-dot-env: the .env parser, file priority, schema, leak guard, build hook, runtime `env` and the CLI.
All secret-looking values here are fake, made up for the tests."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pyxpo import module_hooks
from pyxpo.app_config import resolve
from pyxpo.app_config.schema import ConfigError
from pyxpo.build import app_json
from pyxpo.config import load_config

import pyxpo_dot_env
from pyxpo_dot_env import Env, EnvError, env
from pyxpo_dot_env import __main__ as cli
from pyxpo_dot_env import build, guard, loader, schema
from pyxpo_dot_env.dotenv import DotenvError, dumps_value, expand, parse, resolve as resolve_refs

FAKE_STRIPE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in [k for k in os.environ if k.startswith("PYXPO_PUBLIC_") or k.startswith("PXENV_")]:
        monkeypatch.delenv(k)
    monkeypatch.delenv(loader.SKIP_ENV, raising=False)
    monkeypatch.delenv(module_hooks.MODE_ENV, raising=False)
    yield
    for k, v in list(loader._injected.items()):
        if os.environ.get(k) == v:
            del os.environ[k]
    loader._injected.clear()
    module_hooks._warned.clear()


def values(text):
    return {e.key: e.raw for e in parse(text)}


# parser ------------------------------------------------------------------------------------------------------------

def test_parse_basics():
    v = values("\ufeff# c\n\nexport A=1\nB = two words  # note\nC=#ff0000\nD= # empty\nE=\nF=a#b\r\nG=x=y\n")
    assert v == {"A": "1", "B": "two words", "C": "#ff0000", "D": "", "E": "", "F": "a#b", "G": "x=y"}


def test_parse_quotes():
    text = ("D=\"a\\nb\\t\\\"q\\\" \\\\ \\$5 \\${X}\"\nS='it\\'s ${raw} \\n'\nM=\"one\ntwo\"  # c\n"
            "N='multi\nline'\nAFTER=1\n")
    entries = {e.key: e for e in parse(text)}
    assert entries["D"].raw == 'a\nb\t"q" \\ $5 $${X}' and entries["D"].expand
    assert entries["S"].raw == "it's ${raw} \\n" and not entries["S"].expand
    assert entries["M"].raw == "one\ntwo"
    assert entries["N"].raw == "multi\nline"
    assert entries["AFTER"].line == 7


def test_parse_errors_have_line():
    for text, line, msg in (("A=1\nNOEQUALS\n", 2, "expected '='"), ("1A=x", 1, "KEY=value"),
                            ("A=1\nB=\"open\n", 2, "never closed"), ("A='x' y", 1, "after the closing quote"),
                            ("A-B=1", 1, "expected '='")):
        with pytest.raises(DotenvError, match=r"\.env:%d: .*%s" % (line, msg)):
            parse(text)


def test_duplicate_last_wins():
    from pyxpo_dot_env.loader import load
    assert [e.raw for e in parse("A=1\nA=2\n")] == ["1", "2"]


def test_expand():
    env_ = {"HOST": "api.example.com", "EMPTY": ""}
    look = env_.get
    assert expand("https://${HOST}/v1", look) == "https://api.example.com/v1"
    assert expand("${MISSING:-dflt}|${EMPTY:-e}|${EMPTY-x}|${MISSING-y}", look) == "dflt|e||y"
    assert expand("$${HOST} $HOST $5", look) == "${HOST} $HOST $5"
    assert expand("${MISSING:-${HOST}}", look) == "api.example.com"
    with pytest.raises(DotenvError, match=r"\$\{MISSING\} is not set .*\$\{MISSING:-\}"):
        expand("${MISSING}", look)
    with pytest.raises(DotenvError, match="not a valid reference"):
        expand("${1BAD}", look)
    with pytest.raises(DotenvError, match="never closed"):
        expand("ab${cd", look)


def test_resolve_refs_and_cycles():
    entries = {e.key: e for e in parse("A=${B}/a\nB=${C}\nC=c\nR='${A}'\n")}
    assert resolve_refs(entries, {}) == {"A": "c/a", "B": "c", "C": "c", "R": "${A}"}
    assert resolve_refs(entries, {"C": "env"})["A"] == "env/a"
    loop = {e.key: e for e in parse("X=${Y}\nY=${X}\n")}
    with pytest.raises(DotenvError, match="refers back to itself"):
        resolve_refs(loop, {})


def test_dumps_round_trip():
    for v in ("", "plain", "https://x.example/a?b=c", "two words", "it's", 'q"uote', "a\nb", "back\\slash",
              "${NOT}", "$5"):
        e = parse("K=%s\n" % dumps_value(v))[0]
        got = resolve_refs({"K": e}, {})["K"]
        assert got == v, (v, dumps_value(v))


# loader ------------------------------------------------------------------------------------------------------------

def write(d, **files):
    for name, text in files.items():
        (d / name.replace("_", ".")).write_text(text)


def test_file_priority(tmp_path):
    (tmp_path / ".env").write_text("A=env\nB=env\nC=env\nD=env\nREF=${D}\n")
    (tmp_path / ".env.staging").write_text("B=mode\nC=mode\nD=mode\n")
    (tmp_path / ".env.local").write_text("C=local\nD=local\n")
    (tmp_path / ".env.staging.local").write_text("D=mode-local\n")
    (tmp_path / ".env.production").write_text("A=prod\n")
    got = loader.load(tmp_path, "staging", environ={})
    assert got.values == {"A": "env", "B": "mode", "C": "local", "D": "mode-local", "REF": "mode-local"}
    assert got.sources["D"] == ".env.staging.local" and got.sources["A"] == ".env"
    assert [p.name for p in got.files] == [".env", ".env.staging", ".env.local", ".env.staging.local"]
    real = loader.load(tmp_path, "staging", environ={"D": "ci", "A": "ci"})
    assert real.values["D"] == "ci" and real.values["REF"] == "ci" and real.sources["A"] == "environment"


def test_skip_files_and_local_mode(tmp_path):
    (tmp_path / ".env").write_text("A=1\n")
    assert loader.load(tmp_path, "development", environ={loader.SKIP_ENV: "1"}).values == {}
    with pytest.raises(DotenvError, match="'local'"):
        loader.load(tmp_path, "local", environ={})
    (tmp_path / ".env").write_bytes(b"A=\xff\n")
    with pytest.raises(DotenvError, match="UTF-8"):
        loader.load(tmp_path, "development", environ={})


def test_inject_and_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("PXENV_REAL", "real")
    (tmp_path / ".env").write_text("PXENV_A=1\nPXENV_REAL=file\nPXENV_GONE=x\n")
    done = loader.inject(loader.load(tmp_path, "development"))
    assert done == {"PXENV_A", "PXENV_GONE"} and os.environ["PXENV_A"] == "1"
    assert os.environ["PXENV_REAL"] == "real"
    (tmp_path / ".env").write_text("PXENV_A=2\n")  # an edit while the dev server runs
    loaded = loader.load(tmp_path, "development")
    assert loaded.values["PXENV_A"] == "2" and loaded.sources["PXENV_A"] == ".env"
    loader.inject(loaded)
    assert os.environ["PXENV_A"] == "2" and "PXENV_GONE" not in os.environ


# schema ------------------------------------------------------------------------------------------------------------

def test_coerce():
    assert schema.coerce("int", " 1_000 ") == 1000 and schema.coerce("int", 5) == 5
    assert schema.coerce("float", "2.5") == 2.5
    assert [schema.coerce("bool", s) for s in ("true", "ON", "1", "no", "False", "0")] == [True] * 3 + [False] * 3
    assert schema.coerce("list", "a, b,,c ") == ["a", "b", "c"]
    assert schema.coerce("json", '{"a": [1]}') == {"a": [1]}
    assert schema.coerce("url", "https://api.example.com/v1") == "https://api.example.com/v1"
    assert schema.coerce("str", True) == "true"
    for t, bad in (("int", "1.5"), ("int", True), ("float", "nan"), ("float", "x"), ("bool", "maybe"),
                   ("url", "api.example.com"), ("url", "https://"), ("json", "{bad")):
        with pytest.raises(ValueError):
            schema.coerce(t, bad)


def test_parse_schema():
    vars_, errors = schema.parse_schema({
        "PYXPO_PUBLIC_A": "int", "B": {"type": "bool", "default": "yes"},
        "C": {"type": "nope"}, "D": {"colour": 1}, "E": {"choices": []}, "F": {"pattern": "("},
        "G": {"type": "int", "default": "x"}, "1BAD": "str", "H": 5, "I": {"required": "yes"},
        "J": {"required": []}, "K": {"required": ["production", ""]}, "L": {"required": ["production"]}})
    assert set(vars_) == {"PYXPO_PUBLIC_A", "B", "L"} and vars_["B"].default is True
    assert len(errors) == 10
    assert vars_["L"].required_in("production") and not vars_["L"].required_in("staging")
    assert vars_["B"].required_in("production") is False
    assert schema.parse_schema([1])[1]


def test_check_choices_and_pattern():
    v = schema.Var("X", "str", choices=["light", "dark"])
    assert schema.check(v, "dark") == "dark"
    with pytest.raises(ValueError, match="one of light, dark"):
        schema.check(v, "blue")
    p = schema.Var("Y", "str", pattern=r"[a-z]+-\d+")
    assert schema.check(p, "abc-12") == "abc-12"
    with pytest.raises(ValueError, match="pattern"):
        schema.check(p, "abc-12x")
    n = schema.Var("Z", "int", choices=[1, 2])
    assert schema.check(n, "2") == 2


def test_insecure_url():
    assert schema.insecure_url("http://api.example.com")
    for ok in ("https://api.example.com", "http://localhost:8000", "http://10.0.2.2:8000", "http://my.local",
               "http://127.0.0.1"):
        assert not schema.insecure_url(ok), ok


# guard -------------------------------------------------------------------------------------------------------------

def test_secret_patterns():
    jwt = "eyJhbGciOiJIUzI1NiJ9." + \
        __import__("base64").urlsafe_b64encode(b'{"role":"service_role"}').decode().rstrip("=") + ".sig_part"
    anon = "eyJhbGciOiJIUzI1NiJ9." + \
        __import__("base64").urlsafe_b64encode(b'{"role":"anon"}').decode().rstrip("=") + ".sig_part"
    found = {
        FAKE_AWS: "AWS", FAKE_STRIPE: "Stripe", "rk_test_" + "a" * 24: "Stripe",
        "sk-ant-api03-" + "a" * 30: "Anthropic", "sk-proj-" + "a" * 30: "OpenAI",
        "-----BEGIN RSA PRIVATE KEY-----\nMII": "private key", "ghp_" + "a" * 36: "GitHub",
        "xoxb-" + "1" * 20: "Slack", "GOCSPX-" + "a" * 28: "Google OAuth", "SG." + "a" * 22 + "." + "b" * 43: "SendGrid",
        '{"type": "service_account", "x": 1}': "service account", "postgres://user:pw@db.example.com/x": "password",
        jwt: "service_role",
    }
    for value, word in found.items():
        kind = guard.secret_kind(value)
        assert kind and word in kind, (value, kind)
    for public in ("AIza" + "S" * 35, "pk_live_" + "a" * 24, anon, "https://abc@o1.ingest.sentry.io/1",
                   "https://api.example.com", "123456789-abc.apps.googleusercontent.com", "short"):
        assert guard.secret_kind(public) is None, public


def test_check_public_and_allow():
    assert "Stripe" in guard.check_public("PYXPO_PUBLIC_PAY", FAKE_STRIPE)
    assert FAKE_STRIPE not in guard.check_public("PYXPO_PUBLIC_PAY", FAKE_STRIPE)  # masked in the message
    assert "SECRET" in guard.check_public("PYXPO_PUBLIC_CLIENT_SECRET", "x")
    assert guard.check_public("PYXPO_PUBLIC_CLIENT_SECRET", "x", ["PYXPO_PUBLIC_CLIENT_SECRET"]) is None
    assert guard.check_public("PYXPO_PUBLIC_API_KEY", "AIza" + "S" * 35) is None
    assert guard.mask("abc") == "***" and guard.mask(FAKE_STRIPE).startswith("sk_l") and "(32 chars)" in \
        guard.mask(FAKE_STRIPE)


# analyze / build hook ----------------------------------------------------------------------------------------------

def proj(tmp_path, dotenv="", extra="", options="", modules='["dot-env"]'):
    p = tmp_path / "proj"
    (p / "app").mkdir(parents=True, exist_ok=True)
    (p / "app" / "index.py").write_text("from pyxpo_dot_env import env\ndefault = None\n")
    (p / "pyproject.toml").write_text('[tool.pyxpo]\nname = "Demo"\nmodules = %s\n%s\n%s' % (modules, extra, options))
    if dotenv:
        (p / ".env").write_text(dotenv)
    return p


def test_build_ships_public_only(tmp_path):
    p = proj(tmp_path, "PYXPO_PUBLIC_API_URL=https://api.example.com\nPXENV_SERVER_KEY=build-only-dummy\n",
             '[tool.pyxpo.extra]\napi = "${PYXPO_PUBLIC_API_URL}/v2"\n',
             "[tool.pyxpo.module_options.dot-env.vars]\nPYXPO_PUBLIC_RETRIES = { type = \"int\", default = 2 }\n")
    cfg = load_config(p)
    nc = resolve(cfg, p)
    shipped = app_json.public(nc)
    assert shipped["modules"]["dot-env"] == {"mode": "development", "vars": {
        "PYXPO_PUBLIC_API_URL": "https://api.example.com", "PYXPO_PUBLIC_RETRIES": 2}}
    assert shipped["extra"]["api"] == "https://api.example.com/v2"
    assert "build-only-dummy" not in json.dumps(shipped)
    assert os.environ["PXENV_SERVER_KEY"] == "build-only-dummy"  # app_config.py and build tools see it


def test_build_mode_files(tmp_path):
    p = proj(tmp_path, "PYXPO_PUBLIC_NAME=dev\n")
    (p / ".env.production").write_text("PYXPO_PUBLIC_NAME=prod\n")
    assert resolve(load_config(p, "production"), p)["module_data"]["dot-env"] == {
        "mode": "production", "vars": {"PYXPO_PUBLIC_NAME": "prod"}}
    assert resolve(load_config(p), p)["module_data"]["dot-env"]["vars"]["PYXPO_PUBLIC_NAME"] == "dev"


def test_environment_public_vars_ship(tmp_path, monkeypatch):
    monkeypatch.setenv("PYXPO_PUBLIC_FROM_CI", "ci")
    p = proj(tmp_path)
    assert resolve(load_config(p), p)["module_data"]["dot-env"]["vars"] == {"PYXPO_PUBLIC_FROM_CI": "ci"}


def test_leak_stops_build(tmp_path):
    p = proj(tmp_path, "PYXPO_PUBLIC_STRIPE=%s\nPYXPO_PUBLIC_AWS=%s\n" % (FAKE_STRIPE, FAKE_AWS))
    with pytest.raises(ConfigError) as e:
        load_config(p)
    msg = str(e.value)
    assert "2 problems" in msg and "Stripe" in msg and "AWS" in msg and FAKE_STRIPE not in msg
    p2 = proj(tmp_path / "b", "PYXPO_PUBLIC_STRIPE=%s\n" % FAKE_STRIPE,
              options='[tool.pyxpo.module_options.dot-env]\nallow = ["PYXPO_PUBLIC_STRIPE"]\n')
    load_config(p2)
    p3 = proj(tmp_path / "c", "STRIPE=%s\n" % FAKE_STRIPE)  # build-only: fine
    load_config(p3)


def test_schema_errors_collected(tmp_path):
    p = proj(tmp_path, "PYXPO_PUBLIC_RETRIES=many\nPYXPO_PUBLIC_EXTRA=1\n", options=(
        "[tool.pyxpo.module_options.dot-env]\nstrict = true\n"
        "[tool.pyxpo.module_options.dot-env.vars]\n"
        "PYXPO_PUBLIC_RETRIES = \"int\"\n"
        "PYXPO_PUBLIC_API_URL = { type = \"url\", required = true, description = \"Backend\", "
        "example = \"https://api.example.com\" }\n"
        "PXENV_BUILD_TOKEN = { required = true }\n"))
    with pytest.raises(ConfigError) as e:
        load_config(p)
    msg = str(e.value)
    assert "4 problems" in msg
    assert "PYXPO_PUBLIC_RETRIES must be a whole number" in msg and "(from .env)" in msg
    assert "PYXPO_PUBLIC_API_URL is required but not set (Backend); for example" in msg
    assert "PXENV_BUILD_TOKEN is required" in msg
    assert "PYXPO_PUBLIC_EXTRA is not declared" in msg



def test_required_in_some_modes(tmp_path):
    p = proj(tmp_path, options=(
        "[tool.pyxpo.module_options.dot-env.vars]\n"
        "PYXPO_PUBLIC_ADMOB_ID = { required = [\"production\"], pattern = \"ca-app-pub-\\\\d{16}~\\\\d{10}\", "
        "example = \"ca-app-pub-0000000000000000~0000000000\" }\n"))
    assert "PYXPO_PUBLIC_ADMOB_ID" not in resolve(load_config(p), p)["module_data"]["dot-env"]["vars"]
    load_config(p, "staging")
    with pytest.raises(ConfigError, match="PYXPO_PUBLIC_ADMOB_ID is required in production builds but not set; "
                                          "for example"):
        load_config(p, "production")
    (p / ".env.production").write_text("PYXPO_PUBLIC_ADMOB_ID=\n")       # an empty placeholder is not a value
    with pytest.raises(ConfigError, match="required in production builds"):
        load_config(p, "production")
    (p / ".env.production").write_text("PYXPO_PUBLIC_ADMOB_ID=ca-app-pub-1234567890123456~1234567890\n")
    assert resolve(load_config(p, "production"), p)["module_data"]["dot-env"]["vars"] == {
        "PYXPO_PUBLIC_ADMOB_ID": "ca-app-pub-1234567890123456~1234567890"}
    report = build.analyze(p, "development", cli.read_config(p), environ={})
    assert "required in production" in cli.example_text(report)

def test_unknown_options(tmp_path):
    p = proj(tmp_path, options="[tool.pyxpo.module_options.dot-env]\nstric = true\n")
    with pytest.raises(ConfigError, match="unknown key stric"):
        load_config(p)


def test_interpolation(tmp_path):
    p = proj(tmp_path, "PXENV_BUILD=b\nPYXPO_PUBLIC_A=a\n",
             '[tool.pyxpo.extra]\nleak = "${PXENV_BUILD}"\nlit = "$${NOT}"\n')
    with pytest.raises(ConfigError, match="extra ships inside the app"):
        load_config(p)
    p2 = proj(tmp_path / "b", "PYXPO_PUBLIC_A=a\n", '[tool.pyxpo.extra]\nx = "${NOPE}"\n')
    with pytest.raises(ConfigError, match=r"\[tool.pyxpo\] extra.x: \$\{NOPE\} is not set"):
        load_config(p2)
    p3 = proj(tmp_path / "c", "PXENV_ID=com.example.app\nPYXPO_PUBLIC_A=a\n",
              '[tool.pyxpo.extra]\nlit = "$${NOT}"\nlist = ["${PYXPO_PUBLIC_A}"]\n'
              '[tool.pyxpo.ios]\nbundle_identifier = "${PXENV_ID}"\n')
    cfg = load_config(p3)
    assert cfg["extra"] == {"lit": "${NOT}", "list": ["a"]}
    assert resolve(cfg, p3)["ios"]["bundle_id"] == "com.example.app"


def test_encrypted_values(tmp_path):
    p = proj(tmp_path, 'PYXPO_PUBLIC_A="encrypted:BDqDBibm4wsYqMpCjTQ6BsDHmMadg9K3dAt+Z9HPMfLEIRVz50hmLXPXRuDBXaJi"\n')
    with pytest.raises(ConfigError, match="dotenvx run -- pyxpo build"):
        load_config(p)


def test_warnings(tmp_path, capsys):
    p = proj(tmp_path, "EXPO_PUBLIC_API=x\nPYXPO_PUBLIC_URL=http://api.example.com\n")
    (p / "app" / ".env").write_text("A=1\n")
    load_config(p, "production")
    out = capsys.readouterr()
    text = out.out + out.err
    assert "EXPO_PUBLIC_" in text and "plain http://" in text and "app/.env is inside a bundled folder" in text


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_git_warnings(tmp_path):
    p = proj(tmp_path, "PXENV_DB_PASSWORD=dummy\nPYXPO_PUBLIC_A=a\n")
    run = lambda *a: subprocess.run(["git", "-C", str(p)] + list(a), capture_output=True, check=True)
    run("init", "-q")
    run("add", ".env")
    loaded = loader.load(p, "development", environ={})
    warns = build.git_warnings(p, loaded)
    assert any(".env.local is not in .gitignore" in w for w in warns)
    assert any(".env is committed to git and holds PXENV_DB_PASSWORD" in w for w in warns)
    assert cli.ensure_gitignore(p) == [".env*.local", ".env.keys"]
    assert cli.ensure_gitignore(p) == []
    assert not any("not in .gitignore" in w for w in build.git_warnings(p, loaded))


def test_git_check_option(tmp_path, capsys):
    p = proj(tmp_path, "PXENV_DB_PASSWORD=dummy\n", options='[tool.pyxpo.module_options.dot-env]\ngit_check = false\n')
    subprocess.run(["git", "-C", str(p), "init", "-q"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(p), "add", ".env"], capture_output=True, check=True)
    assert cli.main(["check", "--dir", str(p)]) == 0
    assert "committed to git" not in capsys.readouterr().out
    report = build.analyze(p, "development", {"module_options": {"dot-env": {"git_check": "no"}}}, environ={})
    assert "module_options.dot-env.git_check must be true or false" in report.errors


def test_env_dir_option(tmp_path):
    p = proj(tmp_path, options='[tool.pyxpo.module_options.dot-env]\ndir = "config"\n')
    (p / "config").mkdir()
    (p / "config" / ".env").write_text("PYXPO_PUBLIC_A=from-config\n")
    assert resolve(load_config(p), p)["module_data"]["dot-env"]["vars"] == {"PYXPO_PUBLIC_A": "from-config"}


# runtime -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def shipped(monkeypatch):
    from pyxpo.apis import constants
    data = {"mode": "staging", "vars": {"PYXPO_PUBLIC_API_URL": "https://api.example.com",
                                        "PYXPO_PUBLIC_RETRIES": "3", "PYXPO_PUBLIC_ON": "yes",
                                        "PYXPO_PUBLIC_TAGS": "a,b", "PYXPO_PUBLIC_CFG": {"k": [1]},
                                        "PYXPO_PUBLIC_RATE": 0.5}}
    monkeypatch.setattr(constants, "_load", lambda: {"modules": {"dot-env": data}})
    e = Env()
    return e


def test_runtime_getters(shipped):
    e = shipped
    assert e.url("API_URL") == e.str("PYXPO_PUBLIC_API_URL") == "https://api.example.com"
    assert e.int("RETRIES") == 3 and e.bool("ON") is True and e.list("TAGS") == ["a", "b"]
    assert e.json("CFG") == {"k": [1]} and e.float("RATE") == 0.5
    assert e.int("MISSING", 7) == 7 and e.get("MISSING") is None
    assert e.mode == "staging" and not e.is_production and not e.is_development
    assert "API_URL" in e and "NOPE" not in e and len(e) == 6 and "PYXPO_PUBLIC_ON" in list(e)
    e.get("CFG")["k"].append(2)
    assert e["CFG"] == {"k": [1]}
    with pytest.raises(EnvError, match="PYXPO_PUBLIC_SECRET is not set.*build-only"):
        e.str("SECRET")
    with pytest.raises(EnvError, match="whole number"):
        e.int("API_URL")
    with pytest.raises(EnvError, match="missing PYXPO_PUBLIC_A, PYXPO_PUBLIC_B"):
        e.require("API_URL", "A", "B")
    e.require("API_URL")
    with pytest.raises(EnvError):
        e["NOPE"]


def test_runtime_override(shipped):
    e = shipped
    with e.override(API_URL="http://localhost:8000", RETRIES=None):
        assert e.str("API_URL") == "http://localhost:8000" and "RETRIES" not in e
    assert e.str("API_URL") == "https://api.example.com"


def test_runtime_without_data(monkeypatch):
    from pyxpo.apis import constants
    monkeypatch.setattr(constants, "_load", lambda: {})
    monkeypatch.delenv(module_hooks.DATA_ENV, raising=False)
    e = Env()
    assert e.mode == "development" and e.is_development and len(e) == 0 and e.to_dict() == {}
    assert isinstance(pyxpo_dot_env.env, Env) and pyxpo_dot_env.PREFIX == "PYXPO_PUBLIC_"


# CLI ---------------------------------------------------------------------------------------------------------------

def test_cli_check_show_example(tmp_path, capsys):
    p = proj(tmp_path, "PYXPO_PUBLIC_API_URL=https://api.example.com\nPXENV_TOKEN=dummy-build-only-token\n",
             options='[tool.pyxpo.module_options.dot-env.vars]\n'
                     'PYXPO_PUBLIC_API_URL = { type = "url", required = true, description = "Backend URL", '
                     'example = "https://api.example.com" }\n'
                     'PYXPO_PUBLIC_DEBUG = { type = "bool", default = false }\n')
    assert cli.main(["check", "--dir", str(p)]) == 0
    assert "2 variables ship" in capsys.readouterr().out
    assert cli.main(["show", "--dir", str(p)]) == 0
    out = capsys.readouterr().out
    assert "dummy-build-only-token" not in out and "build-only" in out and "'https://api.example.com'" in out
    assert cli.main(["example", "--dir", str(p)]) == 0
    out = capsys.readouterr().out
    assert "# Backend URL" in out and "PYXPO_PUBLIC_API_URL=https://api.example.com" in out
    assert "PXENV_TOKEN=\n" in out and "PYXPO_PUBLIC_DEBUG=false" in out and "dummy-build-only-token" not in out
    assert cli.main(["example", "--dir", str(p), "--write"]) == 0
    assert (p / ".env.example").is_file()
    (p / ".env").write_text("PYXPO_PUBLIC_PAY=%s\n" % FAKE_STRIPE)
    assert cli.main(["check", "--dir", str(p)]) == 1
    assert "error:" in capsys.readouterr().out
    assert cli.main(["check", "--dir", str(p), "--mode", "Bad"]) == 2
    (p / ".env").write_text("BROKEN\n")
    assert cli.main(["check", "--dir", str(p)]) == 1


def test_cli_init(tmp_path, capsys):
    p = proj(tmp_path)
    (p / ".gitignore").write_text("build/")
    assert cli.main(["init", "--dir", str(p)]) == 0
    assert (p / ".env").is_file() and (p / ".env.example").is_file()
    ignore = (p / ".gitignore").read_text()
    assert ignore.startswith("build/\n") and ".env*.local\n" in ignore and ".env.keys\n" in ignore
    assert cli.main(["init", "--dir", str(p)]) == 0
    assert "already exists" in capsys.readouterr().out
    assert (p / ".gitignore").read_text() == ignore


def test_module_metadata():
    root = Path(pyxpo_dot_env.__file__).parent
    meta = json.loads((root / "pyxpo-module.json").read_text())
    assert meta["name"] == "dot-env" and meta["build"]["hook"] == "pyxpo_dot_env.build:configure"
    assert (root / "py.typed").is_file()
    from pyxpo.build import autolink
    autolink.validate(meta, "pyxpo-module.json")


def test_pyxpo_build_reports_leak_without_traceback(tmp_path, capsys):
    from pyxpo import cli as pyxpo_cli
    p = proj(tmp_path, "PYXPO_PUBLIC_PAY=%s\n" % FAKE_STRIPE)
    assert pyxpo_cli.main(["build", "web", "--dir", str(p)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: dot-env: 1 problem") and "Traceback" not in err and FAKE_STRIPE not in err


def test_web_watch_sees_env_files(tmp_path):
    from pyxpo.build.web import _watch_stamp
    (tmp_path / ".env").write_text("A=1\n")
    (tmp_path / ".env.local").write_text("B=1\n")
    before = _watch_stamp(tmp_path)
    (tmp_path / ".env.local").unlink()  # removing an override must rebuild too
    assert _watch_stamp(tmp_path) != before

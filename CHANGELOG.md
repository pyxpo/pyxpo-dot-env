# Changelog

## 0.1.0a1

- First release: `.env`, `.env.<mode>`, `.env.local`, `.env.<mode>.local` loading (the environment wins),
  `PYXPO_PUBLIC_` variables shipped into the app, typed `env` getters, a schema (with per-mode `required`) in
  `module_options.dot-env.vars`, `${VAR}` in `[tool.pyxpo]`, a build-time leak guard, git hygiene checks (`git_check`), dotenvx
  `encrypted:` detection and the
  `python -m pyxpo_dot_env check|show|example|init` CLI.

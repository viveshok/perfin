# perfin monorepo

Each top-level directory is an independent uv project with its own
`pyproject.toml`, `uv.lock` and `.venv`:

- [perfin/](perfin/README.md) — CLI that syncs recent bank expenses into a
  Google Sheet. Run from within `perfin/`: `uv run perfin.py`.
- [expenses/](expenses/README.md) — employee expenses submission web portal
  (Flask + SQLite + Google Sign-In), deployed at https://expenses.exe.xyz.

`shell.nix` at the root provides the shared dev tooling (python, uv, ruff, ty).

# developer notes

Run checks from within the project directory you're working on:

```sh
nix-shell ../shell.nix --run "ty check"
nix-shell ../shell.nix --run "ruff format ."
```

# Review decisions: nickhuo overnight batch (2026-09-17/18)

One line per PR. Decision = what we do about it.

| PR | Decision |
|---|---|
| #121 | Keep. Remove dead `__main__` block in `canyonos_core/stub_generator.py`. Catch the new `ValueError` in `cli.py` and exit with a message instead of a traceback. |

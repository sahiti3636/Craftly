"""Implementations of the seams in `app/ports.py`.

`stub_*` modules are self-contained fakes over `seed/` so that C1 runs
with no other slice up. `http_platform.py` is the real thing, calling B1
and B2 over HTTP. `registry.py` picks between them from one environment
variable.

Nothing outside this package should import a `stub_` module by name —
routes and templates go through `app.deps`, which goes through
`registry`. The one exception is tests, which are allowed to reach for a
specific implementation on purpose.
"""

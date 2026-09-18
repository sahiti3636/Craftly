"""Jinja set-up and the context every page gets.

`page(request, "browse.html", ...)` is the only way a route renders. It
folds in the things every template needs — the display language, the
basket count, the rupee formatter, the query-string helper — so that no
route has to remember them and no template has to invent them.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import cart, config, deps
from app.money import rupees

templates = Jinja2Templates(directory=str(config.PACKAGE_DIR / "templates"))


def query_string(request: Request, **changes: Any) -> str:
    """Current query string with some keys replaced, for filter links.

    `None` removes a key. Paging and filtering both need this, and doing
    it in the template with string concatenation is how you end up with
    `?q=&page=2&page=3`.
    """
    params = dict(request.query_params)

    # Changing a filter almost always means going back to page one —
    # otherwise a buyer who ticks "textiles" on page 4 lands on an empty
    # page 4 of textiles and concludes there are none.
    if changes.pop("_reset_page", False):
        params.pop("page", None)

    for key, value in changes.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)

    return f"?{urlencode(params)}" if params else ""


templates.env.globals["rupees"] = rupees
templates.env.globals["query_string"] = query_string
templates.env.globals["LANGUAGES"] = config.LANGUAGES


def page(request: Request, template: str, *, status_code: int = 200, **context: Any):
    lang = context.pop("lang", None) or deps.language(request)
    items = cart.read(request.cookies.get(cart.COOKIE_NAME))
    base = {
        "request": request,
        "lang": lang,
        "cart_count": sum(items.values()),
        "using_stubs": config.using_stubs(),
    }
    return templates.TemplateResponse(
        request, template, {**base, **context}, status_code=status_code
    )

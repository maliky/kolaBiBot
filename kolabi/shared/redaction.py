from __future__ import annotations

from sqlalchemy.engine import make_url


def redact_url(raw_url: str | None) -> str:
    """Return an operator-safe URL string for logs and console output."""

    if not raw_url:
        return "-"
    try:
        return make_url(raw_url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid url>"

"""Shared request dependencies.

`require_token` lives here rather than in `app.py` so that every router can depend on it without
importing the application module, which would make the import graph circular. One definition, one
token check, and no router can accidentally ship without it.
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import Header, HTTPException

API_TOKEN = os.environ.get("LEDGERLINE_API_TOKEN", "dev-token")


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, API_TOKEN):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

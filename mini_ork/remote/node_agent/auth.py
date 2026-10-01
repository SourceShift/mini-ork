"""Bearer-token auth dependency for the node-agent HTTP surface.

Every route except ``GET /v1/health`` requires ``Authorization: Bearer <token>``.
The expected token is read from the env var named by ``--token-env``; the
compare uses :func:`hmac.compare_digest` to keep timing constant.

The token is read from ``os.environ`` at request time (NEVER at module
import) so a token rotation never requires a process restart. Nothing
in this module logs the token or writes it to disk.
"""
from __future__ import annotations

import hmac
import os
from typing import Callable

from fastapi import Header, HTTPException, status


def make_bearer_dependency(token_env: str) -> Callable:
    """Return a FastAPI dependency that gates routes with ``Bearer <token>``.

    ``token_env`` is the env-var name chosen by ``--token-env`` on the CLI.
    A misconfigured node-agent (token env unset or empty) MUST refuse every
    non-health request — a missing token is never treated as "open".
    """

    expected_env = token_env

    async def _dep(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        presented = authorization[len("Bearer "):].strip()
        # Read at request time so a fresh token is honored after rotation.
        expected = os.environ.get(expected_env, "")
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="token not configured",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return _dep


__all__ = ["make_bearer_dependency"]
"""Token-based auth middleware for write endpoints (Epic E6).

Implements the auth substrate per the panel-revised plan at
``docs/research/omnigent-vs-mini-ork-panel-synthesis.md``. The panel
dropped live collaboration entirely, but kept the HTTP API surface as
substrate for third-party SDK clients (Python SDK, mobile, etc).

Auth shape: bearer-token, one token per operator. Tokens live in
``${MINI_ORK_HOME:-.mini-ork}/auth-tokens.txt``, one line per token:

    abc123def456...  amir
    def456abc789...  ops-bot

Lines starting with ``#`` and blank lines are ignored.

Fail-closed: when the token file is missing, ALL write endpoints
return 401. Mini-ork's default-deny posture is non-negotiable here.

Public API::

    from mini_ork.web.auth import require_token

    @router.post("/runs/{run_id}/pause", dependencies=[Depends(require_token)])
    def pause(run_id: str) -> dict:
        ...
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException, Request, status


def _token_file_path() -> Path:
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    return Path(home) / "auth-tokens.txt"


def _load_tokens(path: Path) -> dict[str, str]:
    """Returns {token_hex: operator_label}; empty when file absent."""
    if not path.is_file():
        return {}
    tokens: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            token = parts[0]
            label = parts[1] if len(parts) > 1 else "<unlabeled>"
            tokens[token] = label
    except OSError:
        return {}
    return tokens


def require_token(request: Request) -> str:
    """FastAPI dependency. Returns the operator label on success;
    raises 401 otherwise.

    Pulls ``Authorization: Bearer <token>`` from the request headers.
    On missing/invalid token, raises 401 with a generic ``unauthorized``
    detail (no information leak about whether the token was missing
    versus wrong).
    """
    auth_header = request.headers.get("authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
            headers={"WWW-Authenticate": 'Bearer realm="mini-ork"'},
        )
    presented = auth_header[7:].strip()
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )

    tokens = _load_tokens(_token_file_path())
    label = tokens.get(presented)
    if label is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )
    return label


def auth_configured() -> bool:
    """Helper for the serve startup banner: tells operators whether
    the auth-tokens.txt file is present + non-empty.
    """
    return bool(_load_tokens(_token_file_path()))

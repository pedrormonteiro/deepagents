"""GitHub Copilot OAuth device flow authentication.

Implements the OAuth 2.0 Device Authorization Grant (RFC 8628) to authenticate
users with GitHub and exchange the resulting OAuth token for a short-lived
GitHub Copilot API token (valid for ~25 minutes).

The Copilot token and the underlying OAuth token are cached in
`~/.deepagents/github_copilot_token.json`. On subsequent runs the cached OAuth
token is used to silently refresh the Copilot token, so the full device flow is
only required on first use or after the OAuth token is revoked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# GitHub OAuth app client ID for the Copilot Neovim plugin — a public client ID
# widely used by third-party Copilot integrations (no secret required).
_DEVICE_CLIENT_ID = "Iv1.b507a08c87ecfe98"

_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
_COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"  # noqa: S105

_POLL_TIMEOUT = 300  # seconds — stop polling after 5 minutes
_SLOW_DOWN_INCREMENT = 5  # extra seconds added to interval on slow_down response
_TOKEN_GRACE_BUFFER = 30  # seconds — treat near-expiry tokens as expired
_DEFAULT_TOKEN_LIFETIME = 1500  # seconds (~25 min) — fallback when expires_at is absent


class CopilotAuthError(Exception):
    """Raised when GitHub Copilot authentication fails."""


@dataclass
class DeviceCodeInfo:
    """Device code response from GitHub's device authorization endpoint."""

    device_code: str
    """Opaque code sent back to GitHub during polling."""

    user_code: str
    """Human-readable code the user enters at `verification_uri`."""

    verification_uri: str
    """URL the user must visit to authorize the device."""

    expires_in: int
    """Seconds until the device code expires."""

    interval: int
    """Minimum polling interval in seconds."""


def _http_post_json(
    url: str,
    payload: dict[str, str],
    *,
    timeout: int = 10,
) -> dict[str, object]:
    """Perform a synchronous POST and return the parsed JSON response body.

    Args:
        url: Target URL.
        payload: Form-encoded request body.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        CopilotAuthError: On any HTTP or parse error.
    """
    data = urllib.parse.urlencode(payload).encode()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    req = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read())  # type: ignore[return-value]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        msg = f"HTTP {exc.code} from {url}: {body}"
        raise CopilotAuthError(msg) from exc
    except Exception as exc:
        msg = f"Request to {url} failed: {exc}"
        raise CopilotAuthError(msg) from exc


def _http_get_json(
    url: str,
    token: str,
    *,
    timeout: int = 10,
) -> dict[str, object]:
    """Perform a synchronous GET with a Bearer token and return parsed JSON.

    Args:
        url: Target URL.
        token: OAuth token passed as `Authorization: token <token>`.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        CopilotAuthError: On any HTTP or parse error.
    """
    req = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read())  # type: ignore[return-value]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        msg = f"HTTP {exc.code} from {url}: {body}"
        raise CopilotAuthError(msg) from exc
    except Exception as exc:
        msg = f"Request to {url} failed: {exc}"
        raise CopilotAuthError(msg) from exc


async def request_device_code() -> DeviceCodeInfo:
    """Request a device code from GitHub to start the device authorization flow.

    Returns:
        `DeviceCodeInfo` with the user code, verification URL, and polling details.

    Raises:
        CopilotAuthError: If the request fails or GitHub returns an error.
    """
    body = await asyncio.to_thread(
        _http_post_json,
        _DEVICE_CODE_URL,
        {"client_id": _DEVICE_CLIENT_ID, "scope": "read:user"},
    )
    if "error" in body:
        desc = body.get("error_description", body["error"])
        msg = f"Device code request failed: {desc}"
        raise CopilotAuthError(msg)
    return DeviceCodeInfo(
        device_code=str(body["device_code"]),
        user_code=str(body["user_code"]),
        verification_uri=str(body["verification_uri"]),
        expires_in=int(body.get("expires_in", 900)),
        interval=int(body.get("interval", 5)),
    )


async def poll_for_oauth_token(
    device_code: str,
    interval: int,
    expires_in: int,
) -> str:
    """Poll GitHub until the user authorizes the app and return the OAuth token.

    Args:
        device_code: Device code received from `request_device_code`.
        interval: Minimum seconds to wait between poll attempts.
        expires_in: Seconds until the device code expires.

    Returns:
        GitHub OAuth access token string (`gho_xxx`).

    Raises:
        CopilotAuthError: If authorization is denied, times out, or polling fails.
    """
    deadline = time.monotonic() + min(expires_in, _POLL_TIMEOUT)
    poll_interval = interval
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        body = await asyncio.to_thread(
            _http_post_json,
            _ACCESS_TOKEN_URL,
            {
                "client_id": _DEVICE_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )

        if "access_token" in body:
            return str(body["access_token"])

        error = str(body.get("error", ""))
        if error == "slow_down":
            poll_interval += _SLOW_DOWN_INCREMENT
        elif error == "access_denied":
            msg = "GitHub device authorization was denied."
            raise CopilotAuthError(msg)
        elif error not in {"authorization_pending", ""}:
            desc = body.get("error_description", error)
            msg = f"Device flow error: {desc}"
            raise CopilotAuthError(msg)

    msg = "GitHub device authorization timed out. Please try again."
    raise CopilotAuthError(msg)


async def exchange_for_copilot_token(oauth_token: str) -> tuple[str, int]:
    """Exchange a GitHub OAuth token for a short-lived Copilot API token.

    The returned token is valid for approximately 25 minutes and must be set as
    `GITHUB_TOKEN` for `langchain-github-copilot` to authenticate successfully.

    Args:
        oauth_token: GitHub OAuth token obtained from the device flow.

    Returns:
        Tuple of `(copilot_token, expires_at)` where `expires_at` is a Unix
        timestamp (seconds since epoch).

    Raises:
        CopilotAuthError: If the exchange request fails or returns no token.
    """
    body = await asyncio.to_thread(_http_get_json, _COPILOT_TOKEN_URL, oauth_token)
    token = body.get("token")
    if not token:
        msg = f"Copilot token exchange returned no token: {body}"
        raise CopilotAuthError(msg)
    expires_at = int(body.get("expires_at", int(time.time()) + _DEFAULT_TOKEN_LIFETIME))
    return str(token), expires_at


def load_cached_copilot_token(cache_path: Path) -> tuple[str, str] | None:
    """Load a valid Copilot token from the on-disk cache.

    Returns `None` when the cache does not exist, is unreadable, or the Copilot
    token has expired.  When the Copilot token is expired but the OAuth token is
    still present the caller can attempt a silent refresh via
    `exchange_for_copilot_token`.

    Args:
        cache_path: Path to the JSON token cache file.

    Returns:
        `(copilot_token, oauth_token)` when a non-expired Copilot token is
        cached, otherwise `None`.
    """
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        expires_at = int(data.get("expires_at", 0))
        if time.time() < expires_at - _TOKEN_GRACE_BUFFER:
            return str(data["copilot_token"]), str(data["oauth_token"])
    except Exception:
        logger.debug("Failed to load Copilot token cache", exc_info=True)
    return None


def save_copilot_token_cache(
    cache_path: Path,
    oauth_token: str,
    copilot_token: str,
    expires_at: int,
) -> None:
    """Persist the Copilot token and OAuth token to disk.

    The file is created with mode 0o600 (owner read/write only) to limit
    exposure of the tokens.

    Args:
        cache_path: Destination path for the JSON cache file.
        oauth_token: Long-lived GitHub OAuth token used for refreshes.
        copilot_token: Short-lived Copilot API token.
        expires_at: Unix timestamp when the Copilot token expires.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.dumps({
            "oauth_token": oauth_token,
            "copilot_token": copilot_token,
            "expires_at": expires_at,
        })
        # Write via a file descriptor so we can set permissions atomically.
        fd = os.open(str(cache_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
    except Exception:
        logger.debug("Failed to save Copilot token cache", exc_info=True)


async def get_valid_copilot_token(cache_path: Path) -> str | None:
    """Return a valid Copilot API token from cache, refreshing silently if possible.

    Tries the following in order:

    1. Return the cached Copilot token if it has not expired.
    2. If the Copilot token is expired but the OAuth token is cached, exchange it
       for a new Copilot token and update the cache.

    Args:
        cache_path: Path to the JSON token cache file.

    Returns:
        A valid Copilot API token, or `None` if the full device flow is required.
    """
    cached = load_cached_copilot_token(cache_path)
    if cached is not None:
        return cached[0]

    # Copilot token expired — try silent refresh using saved OAuth token.
    exists = await asyncio.to_thread(cache_path.exists)
    if exists:
        try:
            text = await asyncio.to_thread(cache_path.read_text, encoding="utf-8")
            data = json.loads(text)
            oauth_token = data.get("oauth_token")
            if oauth_token:
                logger.debug("Refreshing Copilot token via cached OAuth token")
                copilot_token, expires_at = await exchange_for_copilot_token(
                    str(oauth_token)
                )
                save_copilot_token_cache(
                    cache_path, str(oauth_token), copilot_token, expires_at
                )
                return copilot_token
        except CopilotAuthError:
            logger.debug("Silent Copilot token refresh failed", exc_info=True)
        except (json.JSONDecodeError, OSError):
            logger.debug(
                "Failed to read Copilot token cache for refresh",
                exc_info=True,
            )

    return None

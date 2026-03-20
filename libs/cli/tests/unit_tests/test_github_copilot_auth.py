"""Tests for the GitHub Copilot device flow authentication module."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path  # noqa: TC003  # used in fixture type annotations
from unittest.mock import AsyncMock, patch

import pytest

from deepagents_cli.github_copilot_auth import (
    CopilotAuthError,
    DeviceCodeInfo,
    exchange_for_copilot_token,
    get_valid_copilot_token,
    load_cached_copilot_token,
    poll_for_oauth_token,
    request_device_code,
    save_copilot_token_cache,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    """Return a temporary path for the token cache file."""
    return tmp_path / "github_copilot_token.json"


@pytest.fixture
def valid_cache(cache_path: Path) -> Path:
    """Populate `cache_path` with a non-expired token cache and return it."""
    save_copilot_token_cache(
        cache_path,
        oauth_token="gho_test_oauth",
        copilot_token="ghu_test_copilot",
        expires_at=int(time.time()) + 3600,
    )
    return cache_path


@pytest.fixture
def expired_cache(cache_path: Path) -> Path:
    """Populate `cache_path` with an expired Copilot token and return it."""
    save_copilot_token_cache(
        cache_path,
        oauth_token="gho_test_oauth",
        copilot_token="ghu_test_expired",
        expires_at=int(time.time()) - 60,
    )
    return cache_path


# ---------------------------------------------------------------------------
# DeviceCodeInfo
# ---------------------------------------------------------------------------


class TestDeviceCodeInfo:
    def test_fields_set_correctly(self) -> None:
        info = DeviceCodeInfo(
            device_code="DEV",
            user_code="ABCD-1234",
            verification_uri="https://github.com/login/device",
            expires_in=900,
            interval=5,
        )
        assert info.device_code == "DEV"
        assert info.user_code == "ABCD-1234"
        assert info.verification_uri == "https://github.com/login/device"
        assert info.expires_in == 900
        assert info.interval == 5


# ---------------------------------------------------------------------------
# save / load token cache
# ---------------------------------------------------------------------------


class TestTokenCache:
    def test_roundtrip(self, cache_path: Path) -> None:
        save_copilot_token_cache(
            cache_path,
            oauth_token="gho_abc",
            copilot_token="ghu_xyz",
            expires_at=int(time.time()) + 1000,
        )
        result = load_cached_copilot_token(cache_path)
        assert result is not None
        assert result[0] == "ghu_xyz"
        assert result[1] == "gho_abc"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = load_cached_copilot_token(tmp_path / "nonexistent.json")
        assert result is None

    def test_expired_token_returns_none(self, expired_cache: Path) -> None:
        result = load_cached_copilot_token(expired_cache)
        assert result is None

    def test_token_within_grace_buffer_returns_none(self, cache_path: Path) -> None:
        # expires 20 seconds from now — within the 30-second grace buffer
        save_copilot_token_cache(
            cache_path,
            oauth_token="gho_abc",
            copilot_token="ghu_xyz",
            expires_at=int(time.time()) + 20,
        )
        assert load_cached_copilot_token(cache_path) is None

    def test_file_permissions(self, cache_path: Path) -> None:
        save_copilot_token_cache(
            cache_path,
            oauth_token="gho_abc",
            copilot_token="ghu_xyz",
            expires_at=int(time.time()) + 1000,
        )
        mode = cache_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_corrupt_file_returns_none(self, cache_path: Path) -> None:
        cache_path.write_text("not valid json", encoding="utf-8")
        result = load_cached_copilot_token(cache_path)
        assert result is None


# ---------------------------------------------------------------------------
# request_device_code
# ---------------------------------------------------------------------------


class TestRequestDeviceCode:
    async def test_success(self) -> None:
        mock_response = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        with patch(
            "deepagents_cli.github_copilot_auth._http_post_json",
            return_value=mock_response,
        ):
            info = await request_device_code()

        assert info.device_code == "dev123"
        assert info.user_code == "ABCD-1234"
        assert info.interval == 5

    async def test_github_error_raises(self) -> None:
        mock_response = {
            "error": "not_supported",
            "error_description": "Device flow not supported",
        }
        with (
            patch(
                "deepagents_cli.github_copilot_auth._http_post_json",
                return_value=mock_response,
            ),
            pytest.raises(CopilotAuthError, match="Device code request failed"),
        ):
            await request_device_code()

    async def test_http_error_propagates(self) -> None:
        with (
            patch(
                "deepagents_cli.github_copilot_auth._http_post_json",
                side_effect=CopilotAuthError("HTTP 503"),
            ),
            pytest.raises(CopilotAuthError, match="HTTP 503"),
        ):
            await request_device_code()


# ---------------------------------------------------------------------------
# poll_for_oauth_token
# ---------------------------------------------------------------------------


class TestPollForOauthToken:
    async def test_returns_token_on_success(self) -> None:
        responses = [
            {"error": "authorization_pending"},
            {"access_token": "gho_success"},
        ]
        call_count = 0

        def fake_post(_url: str, _payload: dict, **__: object) -> dict:
            nonlocal call_count
            result = responses[call_count]
            call_count += 1
            return result

        target = "deepagents_cli.github_copilot_auth._http_post_json"
        with (
            patch(target, side_effect=fake_post),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            token = await poll_for_oauth_token("dev123", interval=1, expires_in=30)

        assert token == "gho_success"

    async def test_raises_on_access_denied(self) -> None:
        with (
            patch(
                "deepagents_cli.github_copilot_auth._http_post_json",
                return_value={"error": "access_denied"},
            ),
            patch("asyncio.sleep", new=AsyncMock()),
            pytest.raises(CopilotAuthError, match="denied"),
        ):
            await poll_for_oauth_token("dev123", interval=1, expires_in=30)

    async def test_raises_on_timeout(self) -> None:
        # expires_in=0 means the deadline is already past before we poll
        with (
            patch(
                "deepagents_cli.github_copilot_auth._http_post_json",
                return_value={"error": "authorization_pending"},
            ),
            patch("asyncio.sleep", new=AsyncMock()),
            pytest.raises(CopilotAuthError, match="timed out"),
        ):
            await poll_for_oauth_token("dev123", interval=1, expires_in=0)

    async def test_surfaces_request_failures(self) -> None:
        with (
            patch(
                "deepagents_cli.github_copilot_auth._http_post_json",
                side_effect=CopilotAuthError("HTTP 503"),
            ),
            patch("asyncio.sleep", new=AsyncMock()),
            pytest.raises(CopilotAuthError, match="HTTP 503"),
        ):
            await poll_for_oauth_token("dev123", interval=1, expires_in=30)

    async def test_slow_down_increases_interval(self) -> None:
        from deepagents_cli.github_copilot_auth import _SLOW_DOWN_INCREMENT

        responses = [
            {"error": "slow_down"},
            {"access_token": "gho_ok"},
        ]
        call_count = 0
        sleep_calls: list[float] = []

        def fake_post(_url: str, _payload: dict, **__: object) -> dict:
            nonlocal call_count
            result = responses[call_count]
            call_count += 1
            return result

        def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        target = "deepagents_cli.github_copilot_auth._http_post_json"
        with (
            patch(target, side_effect=fake_post),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await poll_for_oauth_token("dev123", interval=5, expires_in=60)

        # After slow_down the interval increases by _SLOW_DOWN_INCREMENT.
        assert sleep_calls[1] == 5 + _SLOW_DOWN_INCREMENT


# ---------------------------------------------------------------------------
# exchange_for_copilot_token
# ---------------------------------------------------------------------------


class TestExchangeForCopilotToken:
    async def test_success(self) -> None:
        future_ts = int(time.time()) + 1500
        with patch(
            "deepagents_cli.github_copilot_auth._http_get_json",
            return_value={"token": "ghu_copilot", "expires_at": future_ts},
        ):
            token, expires_at = await exchange_for_copilot_token("gho_oauth")

        assert token == "ghu_copilot"
        assert expires_at == future_ts

    async def test_missing_token_raises(self) -> None:
        with (
            patch(
                "deepagents_cli.github_copilot_auth._http_get_json",
                return_value={"expires_at": 0},
            ),
            pytest.raises(CopilotAuthError, match="no token"),
        ):
            await exchange_for_copilot_token("gho_oauth")


# ---------------------------------------------------------------------------
# get_valid_copilot_token
# ---------------------------------------------------------------------------


class TestGetValidCopilotToken:
    async def test_returns_cached_token_when_valid(self, valid_cache: Path) -> None:
        token = await get_valid_copilot_token(valid_cache)
        assert token == "ghu_test_copilot"

    async def test_refreshes_when_copilot_token_expired(
        self, expired_cache: Path
    ) -> None:
        future_ts = int(time.time()) + 1500
        with patch(
            "deepagents_cli.github_copilot_auth.exchange_for_copilot_token",
            new=AsyncMock(return_value=("ghu_refreshed", future_ts)),
        ):
            token = await get_valid_copilot_token(expired_cache)

        assert token == "ghu_refreshed"
        # Cache should be updated.
        result = load_cached_copilot_token(expired_cache)
        assert result is not None
        assert result[0] == "ghu_refreshed"

    async def test_returns_none_when_no_cache_and_refresh_fails(
        self, cache_path: Path
    ) -> None:
        token = await get_valid_copilot_token(cache_path)
        assert token is None

    async def test_returns_none_when_refresh_fails(self, expired_cache: Path) -> None:
        with patch(
            "deepagents_cli.github_copilot_auth.exchange_for_copilot_token",
            new=AsyncMock(side_effect=CopilotAuthError("401")),
        ):
            token = await get_valid_copilot_token(expired_cache)

        assert token is None

    async def test_returns_none_when_refresh_cache_is_corrupt(
        self, cache_path: Path
    ) -> None:
        await asyncio.to_thread(
            cache_path.write_text, "not valid json", encoding="utf-8"
        )

        token = await get_valid_copilot_token(cache_path)

        assert token is None

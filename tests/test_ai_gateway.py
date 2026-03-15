"""Tests for AI gateway configuration.

Verifies that:
  - addon_config exposes llm_base_url and extraction_model fields
  - All LLM callsites (chat, platform data-enrichment) respect the
    configurable base URL rather than hardcoding openrouter.ai
  - The http_clients.openrouter_client uses the configured base URL
"""

from __future__ import annotations

import importlib
from unittest.mock import patch, MagicMock

import pytest


class TestAddonConfigGatewayFields:

    def test_llm_base_url_default(self):
        from scripts.addon_config import AddonConfig
        cfg = AddonConfig(
            _env_file=None,
            db_password="x",
            openrouter_api_key="k",
        )
        assert cfg.llm_base_url == "https://openrouter.ai/api/v1"

    def test_llm_base_url_override(self):
        with patch.dict("os.environ", {"LLM_BASE_URL": "http://litellm:4000/v1"}):
            from scripts.addon_config import AddonConfig
            cfg = AddonConfig(
                _env_file=None,
                db_password="x",
                openrouter_api_key="k",
            )
            assert cfg.llm_base_url == "http://litellm:4000/v1"

    def test_extraction_model_default(self):
        from scripts.addon_config import AddonConfig
        cfg = AddonConfig(
            _env_file=None,
            db_password="x",
            openrouter_api_key="k",
        )
        assert cfg.extraction_model == "google/gemini-2.5-flash"

    def test_extraction_model_override(self):
        with patch.dict("os.environ", {"EXTRACTION_MODEL": "my-local/llama"}):
            from scripts.addon_config import AddonConfig
            cfg = AddonConfig(
                _env_file=None,
                db_password="x",
                openrouter_api_key="k",
            )
            assert cfg.extraction_model == "my-local/llama"


class TestChatAppUsesConfigBaseUrl:

    def test_no_hardcoded_openrouter_url(self):
        """chat_app._get_client must use config.llm_base_url, not a literal."""
        import inspect
        try:
            from scripts.chat_app import _get_client
        except ImportError:
            pytest.skip("chat_app deps not installed")
        src = inspect.getsource(_get_client)
        assert "openrouter.ai" not in src

    def test_client_created_with_config_url(self):
        try:
            import scripts.chat_app as mod
        except ImportError:
            pytest.skip("chat_app deps not installed")
        mod._client = None
        with patch.object(mod, "AsyncOpenAI") as mock_cls:
            mock_cls.return_value = MagicMock()
            mod._get_client()
            call_kwargs = mock_cls.call_args
            assert "base_url" in (call_kwargs.kwargs or {}) or \
                   any("base_url" in str(a) for a in (call_kwargs.args or []))


class TestTelegramBotUsesConfigBaseUrl:

    def test_no_hardcoded_openrouter_url(self):
        """telegram_bot._get_client must use config.llm_base_url, not a literal."""
        import inspect
        try:
            from scripts.telegram_bot import _get_client
        except ImportError:
            pytest.skip("telegram_bot deps not installed")
        src = inspect.getsource(_get_client)
        assert "openrouter.ai" not in src


class TestPlatformExtractorsUseConfig:
    """Data-enrichment modules must not hardcode OpenRouter URL or model."""

    def test_hevy_resolver_no_hardcoded_url(self):
        import inspect, scripts.hevy_exercise_resolver as mod
        src = inspect.getsource(mod)
        assert "openrouter.ai/api/v1/chat/completions" not in src

    def test_hevy_resolver_model_from_config(self):
        from scripts.addon_config import config
        import scripts.hevy_exercise_resolver as mod
        assert mod.LLM_MODEL == config.extraction_model

    def test_ifit_recommend_no_hardcoded_url(self):
        import inspect, scripts.ifit_strength_recommend as mod
        src = inspect.getsource(mod)
        assert "openrouter.ai/api/v1/chat/completions" not in src

    def test_ifit_recommend_model_from_config(self):
        from scripts.addon_config import config
        import scripts.ifit_strength_recommend as mod
        assert mod.LLM_MODEL == config.extraction_model

    def test_ifit_extract_no_hardcoded_url(self):
        import inspect, scripts.ifit_llm_extract as mod
        src = inspect.getsource(mod)
        assert "openrouter.ai/api/v1/chat/completions" not in src

    def test_ifit_extract_model_from_config(self):
        from scripts.addon_config import config
        import scripts.ifit_llm_extract as mod
        assert mod.MODEL == config.extraction_model


class TestHttpClientsUseConfig:

    def test_openrouter_client_uses_config_base_url(self):
        """The shared LLM httpx client must point at the configured URL."""
        import scripts.http_clients as mod
        mod._openrouter = None
        client = mod.openrouter_client()
        from scripts.addon_config import config
        expected_host = config.llm_base_url.split("://")[1].split("/")[0]
        assert expected_host in str(client.base_url) or \
               str(client.base_url) == config.llm_base_url

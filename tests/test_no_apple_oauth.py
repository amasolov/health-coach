"""Verify that Apple OAuth support has been fully removed."""

import importlib


class TestAppleOAuthRemoved:

    def test_oauth_apple_module_does_not_exist(self):
        """The scripts.oauth_apple module should not be importable."""
        with __import__("pytest").raises(ModuleNotFoundError):
            importlib.import_module("scripts.oauth_apple")

    def test_addon_config_has_no_apple_fields(self):
        from scripts.addon_config import AddonConfig

        fields = set(AddonConfig.model_fields.keys())
        apple_fields = {f for f in fields if "apple" in f}
        assert not apple_fields, f"Apple config fields still present: {apple_fields}"

    def test_chat_app_oauth_enabled_ignores_apple(self):
        """_OAUTH_ENABLED should not reference any Apple config."""
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "scripts" / "chat_app.py").read_text()
        assert "apple_oauth" not in src
        assert "AppleOAuthProvider" not in src

from __future__ import annotations

import unittest
from pathlib import Path

from pilot107.web.server import (
    WebConfig,
    WebIdentityMode,
    config_from_env,
    is_safe_demo_user,
    resolve_proxy_user,
    resolve_static_request,
)


class WebServerTests(unittest.TestCase):
    def test_config_from_env_reads_api_base_url(self) -> None:
        config = config_from_env({"PILOT107_WEB_API_BASE_URL": "http://api:8080/"})

        self.assertEqual(config.api_base_url, "http://api:8080")

    def test_web_config_defaults_to_alice(self) -> None:
        self.assertEqual(WebConfig(api_base_url="http://api:8080").demo_user, "alice")

    def test_demo_user_validation(self) -> None:
        self.assertTrue(is_safe_demo_user("alice"))
        self.assertTrue(is_safe_demo_user("bob.1-test"))
        self.assertFalse(is_safe_demo_user("../bob"))
        self.assertFalse(is_safe_demo_user(""))

    def test_config_rejects_unsafe_fixed_user(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe username"):
            WebConfig(
                api_base_url="http://api:8080",
                fixed_user="../bob",
                identity_mode=WebIdentityMode.FIXED_USER,
            )

    def test_demo_mode_accepts_safe_client_selected_user(self) -> None:
        config = WebConfig(api_base_url="http://api:8080")

        self.assertEqual(resolve_proxy_user(config, {"x-pilot107-user": "bob"}), "bob")
        self.assertIsNone(resolve_proxy_user(config, {"X-Pilot107-User": "../bob"}))

    def test_fixed_user_mode_ignores_spoofed_client_identity(self) -> None:
        config = WebConfig(
            api_base_url="http://api:8080",
            fixed_user="alice",
            identity_mode=WebIdentityMode.FIXED_USER,
        )

        self.assertEqual(
            resolve_proxy_user(config, {"X-Pilot107-User": "bob"}),
            "alice",
        )

    def test_config_from_env_reads_fixed_identity_mode(self) -> None:
        config = config_from_env(
            {
                "PILOT107_WEB_API_BASE_URL": "http://api:8080",
                "PILOT107_WEB_IDENTITY_MODE": "fixed_user",
                "PILOT107_WEB_FIXED_USER": "alice",
            }
        )

        self.assertEqual(config.identity_mode, WebIdentityMode.FIXED_USER)
        self.assertEqual(config.fixed_user, "alice")

    def test_fixed_identity_mode_requires_explicit_user(self) -> None:
        with self.assertRaisesRegex(ValueError, "PILOT107_WEB_FIXED_USER is required"):
            config_from_env({"PILOT107_WEB_IDENTITY_MODE": "fixed_user"})

    def test_static_assets_exist(self) -> None:
        static_root = Path(__file__).resolve().parents[1] / "src" / "pilot107" / "web" / "static"

        self.assertTrue((static_root / "index.html").exists())
        self.assertTrue((static_root / "assets" / "app.js").exists())
        self.assertTrue((static_root / "assets" / "styles.css").exists())

    def test_product_routes_fall_back_to_spa_but_missing_assets_do_not(self) -> None:
        static_root = Path(__file__).resolve().parents[1] / "src" / "pilot107" / "web" / "static"

        self.assertEqual(
            resolve_static_request("/runs/run_123", root=static_root),
            static_root / "index.html",
        )
        self.assertIsNone(resolve_static_request("/assets/missing.js", root=static_root))
        self.assertIsNone(resolve_static_request("/../../etc/passwd", root=static_root))


if __name__ == "__main__":
    unittest.main()

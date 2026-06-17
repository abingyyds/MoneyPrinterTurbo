import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import platform


class TestPlatformPublicSurface(unittest.TestCase):
    def test_public_user_uses_neutral_account_field(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = platform.PlatformStore(Path(data_dir))

            with store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, email, subrouter_api_key, default_model,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "user-1",
                        "subrouter-alice",
                        "alice@example.test",
                        "sk-test",
                        "gpt-test",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            user = store.get_user("user-1")

            self.assertIn("account", user)
            self.assertNotIn("subrouter", user)
            self.assertEqual(user["username"], "account-alice")
            self.assertEqual(user["account"]["default_model"], "gpt-test")

    def test_public_error_message_removes_backend_provider_name(self):
        message = platform.public_error_message("SubRouter 登录失败: subrouter upstream unavailable")

        self.assertNotIn("SubRouter", message)
        self.assertNotIn("subrouter", message)
        self.assertIn("平台", message)

    def test_model_gateway_env_takes_precedence(self):
        with patch.dict(
            os.environ,
            {
                "MPT_MODEL_GATEWAY_BASE_URL": "https://gateway.example.test",
                "SUBROUTER_BASE_URL": "https://legacy.example.test",
            },
            clear=False,
        ):
            self.assertEqual(
                platform.default_subrouter_base_url(),
                "https://gateway.example.test",
            )

    def test_extract_items_supports_nested_model_lists(self):
        payload = {"data": {"models": {"rows": [{"model_name": "gpt-test"}]}}}

        self.assertEqual(platform._extract_items(payload), [{"model_name": "gpt-test"}])

    def test_distributor_fetch_models_uses_site_models_before_gateway_models(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = platform.PlatformStore(Path(data_dir))

            with store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, email, subrouter_api_key, subrouter_base_url,
                        subrouter_external_user_id, subrouter_session_cookie,
                        subrouter_distributor_id, subrouter_distributor_slug,
                        subrouter_distributor_name, subrouter_distributor_domain,
                        default_model, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "user-dist",
                        "alice",
                        "alice@example.test",
                        "sk-user-token",
                        "https://gateway.example.test",
                        "42",
                        "session=abc",
                        "dist-1",
                        "alice-site",
                        "Alice Site",
                        "alice.example.test",
                        "",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            response = Mock()
            response.status_code = 200
            response.json.return_value = {
                "success": True,
                "data": [{"model_name": "gpt-site", "type": "chat"}],
            }
            response.text = ""

            with patch("app.services.platform.requests.get", return_value=response) as get:
                payload = store.fetch_models("user-dist")

            self.assertEqual(payload["models"], [{"id": "gpt-site", "type": "text"}])
            self.assertEqual(payload["default_model"], "gpt-site")
            get.assert_called_once()
            url = get.call_args.args[0]
            headers = get.call_args.kwargs["headers"]
            self.assertEqual(url, "https://gateway.example.test/api/dist/site/models")
            self.assertEqual(headers["Host"], "alice.example.test")
            self.assertEqual(headers["Cookie"], "session=abc")
            self.assertEqual(headers["New-Api-User"], "42")

    def test_distributor_fetch_models_refreshes_missing_site_domain(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = platform.PlatformStore(Path(data_dir))

            with store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, email, subrouter_api_key, subrouter_base_url,
                        subrouter_external_user_id, subrouter_session_cookie,
                        subrouter_distributor_id, subrouter_distributor_slug,
                        subrouter_distributor_name, default_model, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "user-dist",
                        "alice",
                        "alice@example.test",
                        "sk-user-token",
                        "https://gateway.example.test",
                        "42",
                        "session=abc",
                        "dist-1",
                        "",
                        "Alice Site",
                        "",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            first_site_response = Mock()
            first_site_response.status_code = 404
            first_site_response.json.return_value = {}
            first_site_response.text = ""

            distributor_response = Mock()
            distributor_response.status_code = 200
            distributor_response.json.return_value = {
                "success": True,
                "data": {
                    "id": "dist-1",
                    "slug": "alice-site",
                    "domain": "alice.example.test",
                    "has_distributor": True,
                },
            }
            distributor_response.text = ""

            second_site_response = Mock()
            second_site_response.status_code = 200
            second_site_response.json.return_value = {
                "success": True,
                "data": {"models": {"rows": [{"model_name": "gpt-site"}]}},
            }
            second_site_response.text = ""

            with patch(
                "app.services.platform.requests.get",
                side_effect=[first_site_response, distributor_response, second_site_response],
            ) as get:
                payload = store.fetch_models("user-dist")

            self.assertEqual(payload["models"], [{"id": "gpt-site", "type": "text"}])
            self.assertEqual(get.call_args_list[2].kwargs["headers"]["Host"], "alice.example.test")
            with store.connect() as conn:
                row = conn.execute("SELECT * FROM users WHERE id = ?", ("user-dist",)).fetchone()
            self.assertEqual(row["subrouter_distributor_domain"], "alice.example.test")

    def test_prepare_distributor_account_prefers_site_models(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = platform.PlatformStore(Path(data_dir))
            login = {
                "base_url": "https://gateway.example.test",
                "external_user_id": "42",
                "username": "alice",
                "email": "alice@example.test",
                "session_cookie": "session=abc",
                "distributor": {
                    "id": "dist-1",
                    "slug": "alice-site",
                    "name": "Alice Site",
                    "domain": "alice.example.test",
                },
            }

            with (
                patch.object(store, "_ensure_subrouterai_key", return_value=("sk-user-token", "key-1")),
                patch.object(
                    store,
                    "_fetch_dist_site_models",
                    return_value=[{"id": "gpt-site", "type": "text"}],
                ) as fetch_site_models,
                patch.object(store, "_fetch_gateway_models") as fetch_gateway_models,
            ):
                user = store._prepare_subrouterai_account(Mock(), login, fallback_username="alice")

            self.assertEqual(user["account"]["account_type"], "dist")
            self.assertEqual(user["account"]["default_model"], "gpt-site")
            self.assertEqual(user["account"]["distributor_domain"], "alice.example.test")
            fetch_site_models.assert_called_once()
            fetch_gateway_models.assert_not_called()


if __name__ == "__main__":
    unittest.main()

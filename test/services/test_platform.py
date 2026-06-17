import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()

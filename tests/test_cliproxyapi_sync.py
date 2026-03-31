import unittest
from unittest import mock

from services.cliproxyapi_sync import _probe_remote_auth, sync_chatgpt_cliproxyapi_status


class DummyAccount:
    def __init__(self, *, email="demo@example.com", token="", extra=None, user_id=""):
        self.email = email
        self.token = token
        self.extra = dict(extra or {})
        self.user_id = user_id


class CliproxyapiSyncTests(unittest.TestCase):
    def test_sync_returns_unreachable_when_service_down(self):
        account = DummyAccount()

        with mock.patch(
            "services.cliproxyapi_sync.list_auth_files",
            side_effect=RuntimeError("CLIProxyAPI 无法连接，请确认服务已启动或 API URL 是否正确：http://127.0.0.1:8317"),
        ):
            result = sync_chatgpt_cliproxyapi_status(account, api_url="http://127.0.0.1:8317", api_key="demo")

        self.assertEqual(result["remote_state"], "unreachable")
        self.assertIn("无法连接", result["message"])

    def test_sync_returns_not_found_when_remote_auth_missing(self):
        account = DummyAccount()

        with mock.patch("services.cliproxyapi_sync.list_auth_files", return_value=[]):
            result = sync_chatgpt_cliproxyapi_status(account, api_url="http://127.0.0.1:8317", api_key="demo")

        self.assertFalse(result["uploaded"])
        self.assertIn("未在 CLIProxyAPI 找到匹配", result["message"])

    def test_sync_uses_matching_codex_auth_and_probe(self):
        account = DummyAccount(email="demo@example.com", user_id="acct-123")
        auth_files = [
            {
                "name": "demo@example.com.json",
                "provider": "codex",
                "email": "demo@example.com",
                "auth_index": "auth-001",
                "status": "active",
                "status_message": "",
                "unavailable": False,
            }
        ]

        with mock.patch("services.cliproxyapi_sync.list_auth_files", return_value=auth_files):
            with mock.patch(
                "services.cliproxyapi_sync._probe_remote_auth",
                return_value={
                    "last_probe_at": "2026-03-31T00:00:00Z",
                    "last_probe_status_code": 200,
                    "last_probe_error_code": "",
                    "last_probe_message": "ok",
                    "remote_state": "usable",
                },
            ):
                result = sync_chatgpt_cliproxyapi_status(account, api_url="http://127.0.0.1:8317", api_key="demo")

        self.assertTrue(result["uploaded"])
        self.assertEqual(result["auth_index"], "auth-001")
        self.assertEqual(result["remote_state"], "usable")

    def test_probe_remote_auth_maps_token_invalidated(self):
        with mock.patch(
            "services.cliproxyapi_sync._request_json",
            return_value={
                "status_code": 401,
                "header": {
                    "X-Openai-Ide-Error-Code": ["token_invalidated"],
                },
                "body": '{"error":{"code":"token_invalidated","message":"Your authentication token has been invalidated."}}',
            },
        ):
            result = _probe_remote_auth("auth-001", "acct-123", api_url="http://127.0.0.1:8317", api_key="demo")

        self.assertEqual(result["last_probe_status_code"], 401)
        self.assertEqual(result["last_probe_error_code"], "token_invalidated")
        self.assertEqual(result["remote_state"], "access_token_invalidated")


if __name__ == "__main__":
    unittest.main()

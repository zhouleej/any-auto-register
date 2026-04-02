import unittest
from unittest import mock

from platforms.chatgpt.refresh_token_registration_engine import (
    RefreshTokenRegistrationEngine,
    SignupFormResult,
)


class DummyEmailService:
    service_type = type("ST", (), {"value": "dummy"})()

    def create_email(self):
        return {"email": "user@example.com", "service_id": "svc-1"}

    def get_verification_code(self, **kwargs):
        return "123456"


class SequenceEmailService(DummyEmailService):
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def get_verification_code(self, **kwargs):
        self.calls.append(kwargs)
        if not self.codes:
            return None
        return self.codes.pop(0)


class EmptyEmailService(DummyEmailService):
    service_type = type("ST", (), {"value": "custom_provider"})()

    def create_email(self):
        return {"email": "   ", "service_id": "svc-empty"}


class _DummyHTTPClient:
    def __init__(self, sessions):
        self._sessions = list(sessions)
        self._index = 0

    @property
    def session(self):
        return self._sessions[self._index]

    def close(self):
        if self._index < len(self._sessions) - 1:
            self._index += 1


class RegistrationEngineFlowTests(unittest.TestCase):
    def _make_engine(self):
        return RefreshTokenRegistrationEngine(
            email_service=DummyEmailService(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda msg: None,
        )

    def test_get_verification_code_excludes_previously_used_codes(self):
        email_service = SequenceEmailService(["111111", "222222"])
        engine = RefreshTokenRegistrationEngine(
            email_service=email_service,
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda msg: None,
        )
        engine.email = "user@example.com"
        engine.email_info = {"email": "user@example.com", "service_id": "svc-1"}
        engine._otp_sent_at = 100.0

        first_code = engine._get_verification_code()
        second_code = engine._get_verification_code()

        self.assertEqual(first_code, "111111")
        self.assertEqual(second_code, "222222")
        self.assertEqual(email_service.calls[0]["exclude_codes"], set())
        self.assertEqual(email_service.calls[1]["exclude_codes"], {"111111"})
        self.assertEqual(engine._used_verification_codes, {"111111", "222222"})

    def test_create_email_rejects_blank_email_from_provider(self):
        engine = RefreshTokenRegistrationEngine(
            email_service=EmptyEmailService(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda msg: None,
        )

        ok = engine._create_email()

        self.assertFalse(ok)
        self.assertIsNone(engine.email)
        self.assertIn("返回空邮箱地址", "\n".join(engine.logs))

    @mock.patch("platforms.chatgpt.refresh_token_registration_engine.seed_oai_device_cookie")
    @mock.patch(
        "platforms.chatgpt.refresh_token_registration_engine.generate_device_id",
        return_value="device-fixed",
    )
    def test_get_device_id_reuses_generated_value_across_auth_flow_reset(
        self, _generate_device_id, mock_seed_cookie
    ):
        engine = self._make_engine()
        first_session = mock.Mock()
        first_session.cookies = mock.Mock()
        first_session.get.return_value = mock.Mock(status_code=200)
        second_session = mock.Mock()
        second_session.cookies = mock.Mock()
        second_session.get.return_value = mock.Mock(status_code=200)
        engine.http_client = _DummyHTTPClient([first_session, second_session])

        engine.oauth_start = mock.Mock(auth_url="https://auth.openai.com/oauth/authorize")
        self.assertTrue(engine._init_session())
        first_did = engine._get_device_id()

        engine._reset_auth_flow()
        engine.oauth_start = mock.Mock(auth_url="https://auth.openai.com/oauth/authorize")
        self.assertTrue(engine._init_session())
        second_did = engine._get_device_id()

        self.assertEqual(first_did, "device-fixed")
        self.assertEqual(second_did, "device-fixed")
        _generate_device_id.assert_called_once()
        self.assertEqual(first_session.get.call_count, 1)
        self.assertEqual(second_session.get.call_count, 1)
        self.assertEqual(
            [call.args for call in mock_seed_cookie.call_args_list],
            [
                (first_session, "device-fixed"),
                (second_session, "device-fixed"),
                (second_session, "device-fixed"),
            ],
        )

    def test_run_restarts_login_after_new_registration(self):
        engine = self._make_engine()

        def fake_create_email():
            engine.email_info = {"email": "user@example.com", "service_id": "svc-1"}
            engine.email = "user@example.com"
            return True

        def fake_complete_token_exchange(result):
            result.account_id = "acct-1"
            result.workspace_id = "ws-1"
            result.access_token = "at"
            result.refresh_token = "rt"
            result.id_token = "id"
            result.password = engine.password or "pw"
            return True

        def fake_restart_login_flow():
            engine._token_acquisition_requires_login = True
            return True, ""

        with mock.patch.object(engine, "_check_ip_location", return_value=(True, "US")), \
            mock.patch.object(engine, "_create_email", side_effect=fake_create_email), \
            mock.patch.object(engine, "_prepare_authorize_flow", return_value=("did", "sentinel")), \
            mock.patch.object(engine, "_submit_signup_form", return_value=SignupFormResult(success=True, page_type="create_account_password")), \
            mock.patch.object(engine, "_register_password", return_value=(True, "pw")) as register_password, \
            mock.patch.object(engine, "_send_verification_code", return_value=True) as send_otp, \
            mock.patch.object(engine, "_get_verification_code", return_value="123456") as get_otp, \
            mock.patch.object(engine, "_validate_verification_code", return_value=True) as validate_otp, \
            mock.patch.object(engine, "_create_user_account", return_value=True) as create_account, \
            mock.patch.object(engine, "_restart_login_flow", side_effect=fake_restart_login_flow) as restart_login, \
            mock.patch.object(engine, "_complete_token_exchange", side_effect=fake_complete_token_exchange) as complete_exchange:
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.account_id, "acct-1")
        self.assertEqual(result.refresh_token, "rt")
        self.assertTrue(result.metadata["token_acquired_via_relogin"])
        register_password.assert_called_once()
        send_otp.assert_called_once()
        get_otp.assert_called_once()
        validate_otp.assert_called_once()
        create_account.assert_called_once()
        restart_login.assert_called_once()
        complete_exchange.assert_called_once()

    def test_run_skips_registration_steps_for_existing_account(self):
        engine = self._make_engine()

        def fake_create_email():
            engine.email_info = {"email": "user@example.com", "service_id": "svc-1"}
            engine.email = "user@example.com"
            return True

        def fake_complete_token_exchange(result):
            result.account_id = "acct-existing"
            result.workspace_id = "ws-existing"
            result.access_token = "at"
            result.refresh_token = "rt"
            result.id_token = "id"
            result.source = "login"
            return True

        def fake_submit_signup_form(*args, **kwargs):
            engine._is_existing_account = True
            engine._otp_sent_at = 1.0
            return SignupFormResult(
                success=True,
                page_type="email_otp_verification",
                is_existing_account=True,
            )

        with mock.patch.object(engine, "_check_ip_location", return_value=(True, "US")), \
            mock.patch.object(engine, "_create_email", side_effect=fake_create_email), \
            mock.patch.object(engine, "_prepare_authorize_flow", return_value=("did", "sentinel")), \
            mock.patch.object(engine, "_submit_signup_form", side_effect=fake_submit_signup_form) as submit_signup, \
            mock.patch.object(engine, "_register_password") as register_password, \
            mock.patch.object(engine, "_send_verification_code") as send_otp, \
            mock.patch.object(engine, "_get_verification_code", return_value="123456") as get_otp, \
            mock.patch.object(engine, "_validate_verification_code", return_value=True) as validate_otp, \
            mock.patch.object(engine, "_create_user_account") as create_account, \
            mock.patch.object(engine, "_restart_login_flow") as restart_login, \
            mock.patch.object(engine, "_complete_token_exchange", side_effect=fake_complete_token_exchange) as complete_exchange:
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "login")
        self.assertFalse(result.metadata["token_acquired_via_relogin"])
        submit_signup.assert_called_once()
        register_password.assert_not_called()
        send_otp.assert_not_called()
        get_otp.assert_not_called()
        validate_otp.assert_not_called()
        create_account.assert_not_called()
        restart_login.assert_not_called()
        complete_exchange.assert_called_once()


if __name__ == "__main__":
    unittest.main()

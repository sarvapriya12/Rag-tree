import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

os.environ.setdefault("DEVHIVE_SECRET", "testing-secret-at-least-32-characters-long")

from app.config import Settings
from app.supabase_auth import send_invitation, verified_user


class SupabaseAuthTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings("sqlite://", "testing-secret-at-least-32-characters-long",
                                 supabase_url="https://sample.supabase.co",
                                 supabase_publishable_key="sb_publishable_test",
                                 supabase_secret_key="sb_secret_test",
                                 site_url="http://127.0.0.1:3000")

    def test_supabase_session_requires_confirmed_email(self):
        with patch("app.supabase_auth.httpx.get", return_value=httpx.Response(200, json={
            "id": "external-user", "email": "person@example.com", "email_confirmed_at": None,
        })):
            with self.assertRaises(HTTPException) as failure:
                verified_user(self.settings, "valid-looking-token")
        self.assertEqual(failure.exception.status_code, 401)

    def test_invite_uses_server_secret_and_redirects_to_callback(self):
        with patch("app.supabase_auth.httpx.post", return_value=httpx.Response(200, json={})) as post:
            send_invitation(self.settings, "new@example.com")
        self.assertEqual(post.call_args.args[0], "https://sample.supabase.co/auth/v1/invite")
        self.assertEqual(post.call_args.kwargs["headers"]["apikey"], "sb_secret_test")
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])
        self.assertEqual(post.call_args.kwargs["params"]["redirect_to"],
                         "http://127.0.0.1:3000/auth/callback")

    def test_existing_supabase_user_gets_magic_link(self):
        with patch("app.supabase_auth.httpx.post", side_effect=[
            httpx.Response(422, json={"msg": "User already registered"}),
            httpx.Response(200, json={}),
        ]) as post:
            send_invitation(self.settings, "existing@example.com")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.args[0], "https://sample.supabase.co/auth/v1/otp")
        self.assertFalse(post.call_args.kwargs["json"]["create_user"])


if __name__ == "__main__":
    unittest.main()

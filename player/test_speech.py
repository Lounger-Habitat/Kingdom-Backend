import base64
import io
import wave
from unittest.mock import Mock, patch

import httpx
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from account.models import GameAccount
from player.speech import validate_wav


def audio(seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x01\x00" * (16000 * seconds))
    return base64.b64encode(output.getvalue()).decode("ascii")


@override_settings(MIMO_API_KEY="test-key-only")
class NpcSpeechTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.account = GameAccount.objects.create(username="speech-test", password="unused")
        self.client.force_authenticate(self.account)

    @patch("player.speech.httpx.post")
    def test_authenticated_wav_uses_mimo_asr_and_returns_editable_text(self, post):
        post.return_value = Mock(json=lambda: {"choices": [{"message": {"content": "  你好，比尔。  "}}]})
        result = self.client.post("/api/player/speech/transcribe", {"audio": audio()}, format="json")
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json(), {"success": True, "text": "你好，比尔。"})
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.xiaomimimo.com/v1/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "mimo-v2.5-asr")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-key-only"})
        self.assertTrue(kwargs["json"]["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,"))
        self.assertNotIn("test-key-only", str(result.content))

    @patch("player.speech.httpx.post")
    def test_anonymous_audio_is_not_forwarded(self, post):
        self.client.force_authenticate(user=None)
        response = self.client.post("/api/player/speech/transcribe", {"audio": audio()}, format="json")
        self.assertEqual(response.status_code, 401)
        post.assert_not_called()

    @patch("player.speech.httpx.post")
    def test_invalid_and_overlong_audio_are_not_forwarded(self, post):
        for encoded in ("bad!", audio(31), "", base64.b64encode(b"RIFFbroken").decode()):
            response = self.client.post("/api/player/speech/transcribe", {"audio": encoded}, format="json")
            self.assertEqual(response.status_code, 400, response.content)
        post.assert_not_called()

    @override_settings(MIMO_API_KEY="")
    @patch("player.speech.httpx.post")
    def test_missing_key_has_actionable_fallback(self, post):
        result = self.client.post("/api/player/speech/transcribe", {"audio": audio()}, format="json")
        self.assertEqual(result.status_code, 503)
        post.assert_not_called()

    @patch("player.speech.httpx.post", side_effect=httpx.ReadTimeout("sensitive upstream body"))
    def test_timeout_does_not_expose_upstream_response(self, post):
        result = self.client.post("/api/player/speech/transcribe", {"audio": audio()}, format="json")
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("sensitive", str(result.content))

    @patch("player.speech.httpx.post")
    def test_no_speech_result_and_rate_limit(self, post):
        post.return_value = Mock(json=lambda: {"choices": [{"message": {"content": ""}}]})
        for _ in range(6):
            result = self.client.post("/api/player/speech/transcribe", {"audio": audio()}, format="json")
            self.assertEqual(result.status_code, 422)
        result = self.client.post("/api/player/speech/transcribe", {"audio": audio()}, format="json")
        self.assertEqual(result.status_code, 429)
        self.assertEqual(post.call_count, 6)

    def test_truncated_wav_rejected(self):
        raw = base64.b64decode(audio())[:-100]
        with self.assertRaises(ValueError):
            validate_wav(base64.b64encode(raw).decode())

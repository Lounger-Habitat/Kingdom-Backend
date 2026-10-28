import json
from datetime import timedelta
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from player.quick_input_generation import QuickInputGenerationError, generate_phrases


CONFIG = dict(DEEPSEEK_API_URL="https://example.invalid/chat/completions", DEEPSEEK_API_KEY="mock-key",
              DEEPSEEK_MODEL="configured-model", NPC_QUICK_INPUT_ENABLED=True,
              NPC_QUICK_INPUT_TIMEOUT_SECONDS=30, NPC_QUICK_INPUT_INTERVAL_SECONDS=60,
              NPC_QUICK_INPUT_MIN_NEW_MESSAGES=5, NPC_QUICK_INPUT_QUIET_SECONDS=30,
              NPC_QUICK_INPUT_COOLDOWN_SECONDS=300, NPC_QUICK_INPUT_BATCH_SIZE=2)


@override_settings(**CONFIG)
class QuickInputGenerationTests(SimpleTestCase):
    def test_missing_configuration_never_calls_http(self):
        with override_settings(DEEPSEEK_API_KEY=""), patch("player.quick_input_generation.httpx.post") as post:
            with self.assertRaisesRegex(QuickInputGenerationError, "^not_configured$"):
                generate_phrases({"name": "Lucy"}, [])
            post.assert_not_called()

    def test_json_request_uses_player_evidence_and_deduplicates_valid_phrases(self):
        content = json.dumps({"phrases": ["最近怎么样？", " 最近怎么样？ ", "我想了解烹饪。"]}, ensure_ascii=False)
        response = httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
        history = [
            {"role": "player", "text": "有什么活？", "source": "manual"},
            {"role": "player", "text": "未修改的推荐不当作习惯", "source": "quick_personalized", "edited": False},
            {"role": "npc", "text": "饭店里有些日常工作。", "source": "npc"},
            {"role": "player", "text": "我想先试试做饭。", "source": "quick_personalized", "edited": True},
        ]
        with patch("player.quick_input_generation.httpx.post", return_value=response) as post:
            self.assertEqual(generate_phrases({"name": "Lucy", "occupation": "restaurateur"}, history),
                             ["最近怎么样？", "我想了解烹饪。"])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "configured-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("未修改的推荐不当作习惯", payload["messages"][1]["content"])
        self.assertIn("我想先试试做饭。", payload["messages"][1]["content"])
        self.assertEqual(post.call_args.kwargs["timeout"], 30)

    def test_timeout_truncated_and_invalid_json_return_only_bounded_error_codes(self):
        history = [{"role": "player", "text": "想聊聊近况。", "source": "voice"}]
        cases = [
            (httpx.ReadTimeout("provider body must not escape"), "provider_timeout"),
            (httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}), "response_incomplete"),
            (httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}), "invalid_json_response"),
        ]
        for result, code in cases:
            with self.subTest(code=code), patch("player.quick_input_generation.httpx.post") as post:
                if isinstance(result, Exception):
                    post.side_effect = result
                else:
                    post.return_value = result
                with self.assertRaisesRegex(QuickInputGenerationError, "^" + code + "$"):
                    generate_phrases({"name": "Lucy"}, history)


@override_settings(**CONFIG)
class QuickInputWorkerTests(TestCase):
    def test_lease_preserves_new_messages_and_failure_keeps_previous_cache(self):
        from account.models import GameAccount
        from inventory.models import NpcDefinition
        from player.models import PlayerNpcQuickInputState
        from player.quick_input_scheduler import run_generation_batch

        player = GameAccount.objects.create(username="quick-input-worker", password="unused")
        npc, _ = NpcDefinition.objects.update_or_create(agent_id="10014", defaults={"name": "Lucy", "is_test_only": False})
        state = PlayerNpcQuickInputState.objects.create(
            player=player, npc=npc, history_revision=5, history_initialized=True,
            latest_activity_at=timezone.now() - timedelta(minutes=2),
            phrases=[{"id": "old", "text": "原来的句子。", "source": "personalized"}],
        )
        history = [{"role": "player", "text": "有什么活？", "source": "manual"}]

        def during_request(*args):
            self.assertEqual(run_generation_batch(limit=1)["claimed"], 0, "Another process cannot claim an active lease.")
            PlayerNpcQuickInputState.objects.filter(pk=state.pk).update(history_revision=6)
            return ["饭店有什么活？", "我想试试做饭。"]

        with patch("player.quick_inputs.history_for_state", return_value=history), \
                patch("player.quick_input_scheduler.generate_phrases", side_effect=during_request):
            self.assertEqual(run_generation_batch(limit=1)["generated"], 1)
        state.refresh_from_db()
        self.assertEqual((state.generated_revision, state.history_revision), (5, 6))
        self.assertIsNone(state.generation_token)
        previous = state.phrases
        with patch("player.quick_inputs.history_for_state", return_value=history), \
                patch("player.quick_input_scheduler.generate_phrases", side_effect=QuickInputGenerationError("provider_timeout")), \
                self.assertLogs("player.quick_input_scheduler", level="WARNING"):
            self.assertEqual(run_generation_batch(force=True, limit=1)["failed"], 1)
        state.refresh_from_db()
        self.assertEqual(state.phrases, previous)
        self.assertEqual(state.generated_revision, 5)
        self.assertEqual(state.last_error, "provider_timeout")
        self.assertGreater(state.next_attempt_at, timezone.now())
        self.assertIsNone(state.generation_token)

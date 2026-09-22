"""Create a fresh disposable fixture: python -m village.tests.setup_player_e2e.

The only permitted settings module and database path are fixed below. Existing
fixtures are never overwritten, and importing this module has no side effects.
"""
import json
import os
import secrets
import uuid
from datetime import timedelta
from pathlib import Path


SETTINGS_MODULE = "config.settings_player_e2e"
E2E_DIRECTORY = Path(__file__).resolve().parents[3] / "Temp"


def main():
    configured = os.environ.get("DJANGO_SETTINGS_MODULE")
    if configured not in (None, "", SETTINGS_MODULE):
        raise SystemExit("Refusing settings other than config.settings_player_e2e.")
    from django.conf import settings
    if settings.configured:
        raise SystemExit("Run setup as a separate python -m process, not inside a configured Django process.")

    database_path = E2E_DIRECTORY / "village-player-e2e.sqlite3"
    key_path = E2E_DIRECTORY / "village-player-e2e.key"
    manifest_path = E2E_DIRECTORY / "village-player-e2e.json"
    # This check precedes even secret creation and Django initialization. A retry
    # cannot reset a world or invalidate credentials used by a running fixture.
    if any(path.exists() for path in (database_path, key_path, manifest_path)):
        raise SystemExit("Existing E2E fixture found in Temp; stop it and explicitly remove its test files before a fresh setup.")
    E2E_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with key_path.open("x", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(64))
    os.environ["DJANGO_SETTINGS_MODULE"] = SETTINGS_MODULE
    os.environ["DISABLE_BACKGROUND_SCHEDULERS"] = "true"

    # Resolve and validate configuration before django.setup or migrations can
    # initialize any app. No caller-provided DB path or default settings fallback.
    from config import settings_player_e2e as isolated
    database = isolated.DATABASES["default"]
    if (database["ENGINE"] != "django.db.backends.sqlite3"
            or Path(database["NAME"]).resolve() != database_path.resolve()
            or isolated.DISABLE_BACKGROUND_SCHEDULERS is not True):
        raise SystemExit("Refusing a database or scheduler configuration outside the isolated E2E fixture.")

    import django
    django.setup()
    from django.core.management import call_command
    from django.db import transaction
    from rest_framework_simplejwt.tokens import AccessToken
    from account.models import GameAccount
    from game_time.models import GlobalGameTimeState
    from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory
    from items.models import Item
    from player.models import PlayerState
    from village.models import NpcProfile, VillageLocationDefinition, VillageSimulationConfig
    from village.personal_commissions import publish_commission

    call_command("migrate", interactive=False, verbosity=0)

    def token(account, env=False):
        value = AccessToken.for_user(account)
        value.set_exp(lifetime=timedelta(hours=6))
        if env:
            value["scope"], value["aud"] = "env_server", "kingdom-env"
        return str(value)

    with transaction.atomic():
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(
            village_economy_enabled=True, village_story_enabled=True))
        clock = GlobalGameTimeState.get_instance()
        clock.apply_world_minute(100)
        clock.time_revision = 1
        clock.save()
        Item.objects.update_or_create(pk=1030, defaults=dict(item_name="玉米", item_type="蔬菜", item_price=5))
        npc, _ = NpcDefinition.objects.get_or_create(agent_id="10014", defaults=dict(name="Lucy"))
        NpcGlobalInventory.objects.update_or_create(npc=npc, defaults=dict(money=1000))
        VillageLocationDefinition.objects.get_or_create(location_code="RESTAURANT", defaults=dict(
            display_name="Lucy 饭店", center_anchor_code="RESTAURANT", location_type="PUBLIC"))
        profile, _ = NpcProfile.objects.get_or_create(npc=npc, defaults=dict(display_name="Lucy",
            occupation="饭店经营者", home_location_code="RESTAURANT", work_location_code="RESTAURANT",
            social_location_code="RESTAURANT"))
        if "PUBLISH_PERSONAL_COMMISSION" not in profile.capabilities:
            profile.capabilities = [*profile.capabilities, "PUBLISH_PERSONAL_COMMISSION"]
            profile.save(update_fields=["capabilities"])
        env = GameAccount.objects.create(username="e2e-env-service", is_ai=True)
        players = []
        for index in range(2):
            account = GameAccount.objects.create(username=f"e2e-player-{index+1}",
                appearance={"schemaVersion": 1, "modelId": "artisan_male_01" if index == 0 else "artisan_female_01"},
                appearance_revision=1)
            bag = InventoryBag.objects.create(player=account, character_id=account.username, money=100*(index+1))
            slot = InventorySlot.objects.create(bag=bag, slot_index=0, item_id=1030, item_amount=8,
                quality=3, produced_world_minute=90, expires_world_minute=2980)
            PlayerState.objects.create(player_id=str(account.pk), action_points=200)
            # Exercise the real publication service: each complete commission has
            # one assignee and its own prepaid reward, rather than shared claims.
            commission = publish_commission(npc, dict(title=f"隔离测试食材委托 {index+1}",
                description="在饭店交付一份合格玉米，全部完成后领取金币。", reward_gold=7 + index * 4,
                valid_for_minutes=1440, default_location_code="RESTAURANT",
                stages=[dict(title="交付食材", conditions=[dict(type="item", item_ids=[1030], target=1,
                    min_quality=3, requires_freshness=True, min_remaining_minutes=120)])]), uuid.uuid4(), 100)
            players.append(dict(player_id=str(account.pk), username=account.username, token=token(account),
                instance_id=str(slot.instance_id), commission_id=commission["commission_id"],
                condition_id=commission["stages"][0]["conditions"][0]["id"],
                commission_reward_gold=commission["reward_gold"]))
        manifest = dict(backend_url="http://127.0.0.1:18088/", env_token=token(env, True),
                        players=players, commission_item_id=1030, commission_quantity=1)
    # Publish credentials only after the complete fixture transaction committed.
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream)
    print("Isolated E2E database migrated and two-player fixture saved; credentials not logged.")


if __name__ == "__main__":
    main()

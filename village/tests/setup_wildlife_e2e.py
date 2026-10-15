"""Add a real-hunt fixture to the existing disposable player E2E database.

Run in its own process: python -m village.tests.setup_wildlife_e2e.
No production settings, file removal, fixture reset, or credential logging.
"""
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path


def main():
    module = "config.settings_player_e2e"
    if os.environ.get("DJANGO_SETTINGS_MODULE") not in (None, "", module):
        raise SystemExit("Refusing non-isolated settings")
    os.environ["DJANGO_SETTINGS_MODULE"] = module
    os.environ["DISABLE_BACKGROUND_SCHEDULERS"] = "true"
    from config import settings_player_e2e as isolated
    directory = Path(__file__).resolve().parents[3] / "Temp"
    if Path(isolated.DATABASES['default']['NAME']).resolve() != (directory/'village-player-e2e.sqlite3').resolve():
        raise SystemExit("Refusing database outside Temp")
    if not (directory/'village-player-e2e.sqlite3').exists():
        raise SystemExit("First run setup_player_e2e to create the isolated database and signing key")
    import django
    django.setup()
    from django.core.management import call_command
    from django.db import transaction
    from django.utils import timezone
    from rest_framework_simplejwt.tokens import AccessToken
    from account.models import GameAccount
    from game_time.models import GlobalGameTimeState
    from inventory.models import InventoryBag
    from player.models import PlayerState
    from village.models import VillageSimulationConfig
    from village.wildlife import execute
    call_command('migrate', interactive=False, verbosity=0)
    def token(account, env=False):
        value = AccessToken.for_user(account)
        value.set_exp(lifetime=timedelta(hours=6))
        if env:
            value['scope'], value['aud'] = 'env_server', 'kingdom-env'
        return str(value)
    with transaction.atomic():
        VillageSimulationConfig.objects.update_or_create(config_key='default', defaults={'village_economy_enabled':True})
        clock = GlobalGameTimeState.get_instance()
        env, _ = GameAccount.objects.get_or_create(username='e2e-env-service', defaults={'is_ai':True})
        suffix = uuid.uuid4().hex[:8]
        players = []
        for index in (1, 2):
            account = GameAccount.objects.create(username=f'e2e-hunter-{suffix}-{index}',
                appearance={'schemaVersion':1,'modelId':'artisan_male_01'}, appearance_revision=1)
            InventoryBag.objects.create(player=account, character_id=account.username)
            PlayerState.objects.create(player_id=str(account.pk), action_points=0,
                                       action_points_recovered_at=timezone.now()+timedelta(days=1))
            players.append(dict(player_id=str(account.pk), token=token(account), username=account.username))
        prey_id = str(uuid.uuid4())
        result = execute(dict(action='register',request_id=str(uuid.uuid4()),prey_id=prey_id,
                              species='PIG',region_id='HUNTING_GROUNDS',position={'x':0,'y':0,'z':2}))
        if not result['success']:
            raise RuntimeError(result['result_code'])
        manifest = dict(backend_url='http://127.0.0.1:18088/',env_token=token(env,True),
                        hunting_players=players,hunting_prey_id=prey_id,world_minute=clock.world_minute)
    target = directory/'village-hunting-e2e.json'
    target.write_text(json.dumps(manifest), encoding='utf-8')
    print(f'Isolated hunting fixture ready: {target}; AP=0; credentials not logged.')


if __name__ == '__main__':
    main()

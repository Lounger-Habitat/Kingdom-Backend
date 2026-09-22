"""python -m account.setup_appearance_e2e; creates only a disposable test database."""
import json
import os
from pathlib import Path


def main():
    folder = Path(__file__).resolve().parents[2] / "Temp"
    database = folder / "appearance-e2e.sqlite3"
    manifest = folder / "appearance-e2e.json"
    if database.exists() or manifest.exists():
        raise SystemExit("Existing appearance fixture retained; use it or explicitly remove its test files before recreating.")
    if os.environ.get("DJANGO_SETTINGS_MODULE") not in (None, "", "config.settings_appearance_e2e"):
        raise SystemExit("Refusing non-isolated settings")
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings_appearance_e2e"
    import django
    django.setup()
    from django.core.management import call_command
    from account.models import GameAccount
    from inventory.models import InventoryBag
    from player.models import PlayerState
    from rest_framework_simplejwt.tokens import AccessToken
    from datetime import timedelta
    call_command("migrate", interactive=False, verbosity=0)
    def token(account, env=False):
        t = AccessToken.for_user(account); t.set_exp(lifetime=timedelta(hours=6))
        if env: t["scope"], t["aud"] = "env_server", "kingdom-env"
        return str(t)
    service = GameAccount.objects.create(username="appearance-env", is_ai=True)
    players = []
    for i in range(3):
        # Third account deliberately has no saved appearance to verify the entry gate.
        account = GameAccount.objects.create(username=f"appearance-e2e-{i}",
            appearance=None if i == 2 else {"schemaVersion": 1, "modelId": "artisan_male_01" if i == 0 else "artisan_female_01"},
            appearance_revision=0 if i == 2 else 1)
        InventoryBag.objects.create(player=account, character_id=account.username, money=100)
        PlayerState.objects.create(player_id=str(account.pk), action_points=200)
        players.append({"player_id": str(account.pk), "token": token(account)})
    manifest.write_text(json.dumps({"backend_url": "http://127.0.0.1:18089/", "env_token": token(service, True), "players": players}), encoding="utf-8")
    print("Created isolated appearance fixture; credentials not logged.")


if __name__ == "__main__":
    main()

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from player.quick_input_generation import generation_configured
from player.quick_input_scheduler import run_generation_batch


class Command(BaseCommand):
    help = "Generate cached NPC quick phrases once for one player/NPC (uses the configured DeepSeek API)."

    def add_arguments(self, parser):
        parser.add_argument("--player-id", type=int, required=True)
        parser.add_argument("--npc-id", required=True)
        parser.add_argument("--force", action="store_true", help="Ignore quiet/cooldown/message thresholds; still requires autonomous history and an available lease.")

    def handle(self, *args, **options):
        if not generation_configured():
            raise CommandError("Set DEEPSEEK_API_URL, DEEPSEEK_API_KEY and DEEPSEEK_MODEL, and enable NPC_QUICK_INPUT_ENABLED first.")
        try:
            result = run_generation_batch(player_id=options["player_id"], npc_id=options["npc_id"],
                                          force=options["force"], limit=1)
        except DatabaseError:
            raise CommandError("database_unavailable: apply player migrations before generating.") from None
        self.stdout.write(" ".join(f"{name}={count}" for name, count in result.items()))
        if not result["claimed"]:
            self.stdout.write("No eligible history, cooldown pending, or another worker owns the lease.")

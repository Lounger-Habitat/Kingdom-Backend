from django.core.management.base import BaseCommand
from restaurant.models import PlayerRestaurantState, CustomerTransaction


class Command(BaseCommand):
    help = "重新计算所有玩家的累计好评数（评分>=3.8）"

    def handle(self, *args, **options):
        states = PlayerRestaurantState.objects.all()
        for state in states:
            good_count = CustomerTransaction.objects.filter(
                player=state.player,
                rating__gte=3.8,
            ).count()
            old = state.total_ratings_received
            state.total_ratings_received = good_count
            state.save()
            self.stdout.write(
                f"玩家 {state.player.username}: {old} -> {good_count}"
            )
        self.stdout.write(self.style.SUCCESS("好评数重算完成！"))

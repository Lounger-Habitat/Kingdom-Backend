"""Only accounts created after the arrival migration receive the opening."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from account.models import GameAccount
from .models import PlayerArrivalState


@receiver(post_save, sender=GameAccount, dispatch_uid="player.initialize_arrival")
def initialize_arrival(sender, instance, created, raw=False, using="default", **kwargs):
    if created and not raw:
        PlayerArrivalState.objects.using(using).get_or_create(
            player_id=instance.pk, defaults={"eligible": not instance.is_ai})

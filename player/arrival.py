"""Account-owned arrival presentation; it cannot grant assets or teach controls."""
from django.db import transaction
from django.utils import timezone

from .models import PlayerArrivalState

VERSION = 1
CONTROL_PARAMS = {"arrival_begin": set(), "arrival_finish": {"reason"}}
FINISH_REASONS = {"completed", "skipped", "fallback", "interrupted"}


class ArrivalError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def arrival_overview(player):
    # No row means pre-feature / bulk-imported account, never a new arrival.
    state = PlayerArrivalState.objects.filter(player=player).first()
    eligible = bool(state and state.eligible and not player.is_ai)
    started = bool(state and state.started_at)
    finished = bool(state and state.finished_at)
    return dict(version=VERSION, player_id=str(player.pk),
        revision=state.revision if state else 0, eligible=eligible,
        started=started, finished=finished, should_play=eligible and not started,
        finish_reason=state.finish_reason if state else "")


@transaction.atomic
def control(player, action, params):
    if action not in CONTROL_PARAMS or not isinstance(params, dict) or set(params) - CONTROL_PARAMS[action]:
        raise ArrivalError("INVALID_PARAMS", "入村开场参数无效。")
    reason = params.get("reason", "completed")
    if action == "arrival_finish" and (not isinstance(reason, str) or reason not in FINISH_REASONS):
        raise ArrivalError("INVALID_PARAMS", "入村开场结束方式无效。")
    state = PlayerArrivalState.objects.select_for_update().filter(player=player).first()
    started_now = finished_now = False
    if state and state.eligible and not player.is_ai:
        if action == "arrival_begin" and not state.started_at:
            # Durable before the server assigns a bed or the client shows it.
            # A later connection sees started=True even if the opening crashed.
            state.started_at = timezone.now()
            state.revision += 1
            state.save(update_fields=["started_at", "revision"])
            started_now = True
        elif action == "arrival_finish" and not state.finished_at:
            if not state.started_at:
                raise ArrivalError("ARRIVAL_NOT_STARTED", "入村开场尚未开始。")
            state.finished_at = timezone.now()
            state.finish_reason = reason
            state.revision += 1
            state.save(update_fields=["finished_at", "finish_reason", "revision"])
            finished_now = True
    return dict(arrival_started_now=started_now, arrival_finished_now=finished_now)

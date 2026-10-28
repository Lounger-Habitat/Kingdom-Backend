"""Low-frequency generation with a database lease shared by all Backend processes."""
import logging
import os
import sys
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, close_old_connections
from django.db.models import F, Q
from django.utils import timezone

from .quick_input_generation import QuickInputGenerationError, generate_phrases, generation_configured

logger = logging.getLogger(__name__)
_scheduler = None


def _eligible(now, *, force=False):
    from .models import PlayerNpcQuickInputState

    lease_seconds = max(60, float(settings.NPC_QUICK_INPUT_TIMEOUT_SECONDS) * 2
                        + int(settings.NPC_QUICK_INPUT_INTERVAL_SECONDS))
    query = PlayerNpcQuickInputState.objects.filter(
        Q(generation_token__isnull=True) | Q(generation_started_at__isnull=True)
        | Q(generation_started_at__lte=now - timedelta(seconds=lease_seconds)),
    )
    if force:
        return query.filter(history_revision__gt=0)
    return query.filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
        Q(latest_activity_at__isnull=True)
        | Q(latest_activity_at__lte=now - timedelta(seconds=max(0, int(settings.NPC_QUICK_INPUT_QUIET_SECONDS)))),
        Q(generated_at__isnull=True)
        | Q(generated_at__lte=now - timedelta(seconds=max(0, int(settings.NPC_QUICK_INPUT_COOLDOWN_SECONDS)))),
        history_revision__gte=F("generated_revision") + max(1, int(settings.NPC_QUICK_INPUT_MIN_NEW_MESSAGES)),
    )


def run_generation_batch(*, player_id=None, npc_id=None, force=False, limit=None):
    from .models import PlayerNpcQuickInputState
    from .quick_inputs import history_for_state, sanitize_phrases

    counts = {"claimed": 0, "generated": 0, "failed": 0, "stale": 0}
    if not generation_configured():
        return counts
    batch_size = max(1, min(10, int(limit or settings.NPC_QUICK_INPUT_BATCH_SIZE)))
    now = timezone.now()
    candidates = _eligible(now, force=force)
    if player_id is not None:
        candidates = candidates.filter(player_id=player_id)
    if npc_id is not None:
        candidates = candidates.filter(npc__agent_id=str(npc_id))
    ids = list(candidates.order_by("latest_activity_at", "pk").values_list("pk", flat=True)[:batch_size * 4])
    for state_id in ids:
        if counts["claimed"] >= batch_size:
            break
        started = timezone.now()
        token = uuid.uuid4()
        # A single conditional UPDATE wins the lease without holding a lock during HTTP.
        claimed = _eligible(started, force=force).filter(pk=state_id).update(
            generation_token=token, generation_started_at=started, last_error="",
        )
        if not claimed:
            continue
        counts["claimed"] += 1
        try:
            state = PlayerNpcQuickInputState.objects.select_related("npc", "npc__village_profile").get(
                pk=state_id, generation_token=token,
            )
            revision = state.history_revision
            profile = getattr(state.npc, "village_profile", None)
            npc = {"id": str(state.npc.agent_id),
                   "name": profile.display_name if profile else state.npc.name,
                   "occupation": profile.occupation if profile else "村庄居民"}
            phrases = sanitize_phrases(generate_phrases(npc, history_for_state(state, limit=50)))
            if not phrases:
                raise QuickInputGenerationError("invalid_phrases")
            updated = PlayerNpcQuickInputState.objects.filter(pk=state_id, generation_token=token).update(
                phrases=phrases, version=F("version") + 1, generated_revision=revision,
                generated_at=timezone.now(), generation_token=None, generation_started_at=None,
                next_attempt_at=None, last_error="",
            )
            counts["generated" if updated else "stale"] += 1
        except Exception as failure:
            code = str(failure) if isinstance(failure, QuickInputGenerationError) else "generation_failed"
            code = code[:64]
            PlayerNpcQuickInputState.objects.filter(pk=state_id, generation_token=token).update(
                generation_token=None, generation_started_at=None, last_error=code,
                next_attempt_at=timezone.now() + timedelta(seconds=max(
                    30, int(settings.NPC_QUICK_INPUT_COOLDOWN_SECONDS), int(settings.NPC_QUICK_INPUT_INTERVAL_SECONDS),
                )),
            )
            counts["failed"] += 1
            logger.warning("NPC quick-input generation failed (%s)", code)
    return counts


def _tick():
    close_old_connections()
    try:
        run_generation_batch()
    except DatabaseError:
        logger.warning("NPC quick-input generation skipped (database_unavailable)")
    except Exception:
        logger.warning("NPC quick-input generation skipped (worker_failed)")
    finally:
        close_old_connections()


def start_scheduler():
    global _scheduler
    if _scheduler is not None or settings.DISABLE_BACKGROUND_SCHEDULERS or not generation_configured():
        return
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    skipped = {"test", "migrate", "makemigrations", "check", "shell", "dbshell", "showmigrations",
               "collectstatic", "generate_npc_quick_inputs", "reset_village_world"}
    if command in skipped or "pytest" in os.path.basename(sys.argv[0]):
        return
    if "manage.py" in os.path.basename(sys.argv[0]) and command != "runserver":
        return
    if "runserver" in sys.argv and "--noreload" not in sys.argv and os.environ.get("RUN_MAIN") != "true":
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from config.scheduler_registry import register_scheduler

    scheduler = BackgroundScheduler(timezone=settings.TIME_ZONE)
    scheduler.add_job(
        _tick, "interval", seconds=max(5, int(settings.NPC_QUICK_INPUT_INTERVAL_SECONDS)),
        id="npc_quick_input_generation", name="NPC 常用对话生成", max_instances=1, coalesce=True,
        misfire_grace_time=30, replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    register_scheduler("NPC 常用对话", scheduler)

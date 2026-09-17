"""Small authoritative dialogue lifecycle built on the existing NPC reservation lane."""

from typing import Any, Dict, Optional

from django.db import transaction
from django.db.models import Q

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition

from .models import (
    NpcActionReservation,
    NpcDialogueSession,
    NpcRuntimeState,
    NpcSocialDailyCounter,
    VillageSimulationConfig,
)
from .phase2_services import MINUTES_PER_DAY, NEEDS_FIELDS, NeedsService, public_needs
from .runtime_services import ReservationService, ResultCode


def _result(success: bool, code: str, **data: Any) -> Dict[str, Any]:
    return {"success": success, "result_code": code, **data}


class DialogueService:
    MAX_DURATION_MINUTES = 480
    MIN_CREDITED_DURATION_MINUTES = 30
    MAX_TEXT_LENGTH = 100

    # Unity DialogueSettings_SO.MaxChatDistance 的镜像值，用于规则文本与失败指引。
    # ponytail: 双端常量无法自动同步；改 Unity 资产（Assets/Scripts/Agent/DialogueSettings_SO.cs）
    # 时必须同步这里，否则 GET_RULE 会教错距离。
    MAX_CHAT_DISTANCE_METERS = 4

    @staticmethod
    def _summary(session: NpcDialogueSession) -> Dict[str, Any]:
        return {
            "conversation_id": str(session.conversation_id),
            "status": session.status,
            "topic": session.topic or None,
            "participant_npc_ids": sorted((str(session.initiator.agent_id), str(session.target.agent_id))),
            "started_world_minute": session.started_world_minute,
            "expires_world_minute": session.expires_world_minute,
            "ended_world_minute": session.ended_world_minute,
            "end_reason": session.end_reason or None,
            "message_count": len(session.messages or []),
            "end_requests": list(session.end_requests or []),
            "duration_minutes": None if session.ended_world_minute is None else max(
                0, session.ended_world_minute - session.started_world_minute
            ),
            "valid": session.valid_for_social_needs,
            "social_multiplier_percent": session.social_multiplier_percent,
        }

    @staticmethod
    def active_for_npc(npc) -> Optional[NpcDialogueSession]:
        return NpcDialogueSession.objects.select_related("initiator", "target", "reservation").filter(
            Q(initiator=npc) | Q(target=npc), status="ACTIVE"
        ).first()

    @classmethod
    @transaction.atomic
    def start(cls, npc_id: str, target_npc_id: str, topic: str = "",
              conversation_id: Optional[str] = None) -> Dict[str, Any]:
        npc_id, target_npc_id = str(npc_id or ""), str(target_npc_id or "")
        conversation_id = str(conversation_id or "").strip()
        if (not conversation_id or len(conversation_id) > 128 or not npc_id
                or not target_npc_id or npc_id == target_npc_id
                or not isinstance(topic, str) or len(topic) > 120):
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        if not VillageSimulationConfig.objects.filter(
            config_key="default", village_needs_enabled=True
        ).exists():
            return _result(False, ResultCode.VILLAGE_NEEDS_DISABLED)

        try:
            time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        except GlobalGameTimeState.DoesNotExist:
            return _result(False, ResultCode.BACKEND_UNAVAILABLE)
        cls.expire_active(time_state.world_minute)

        # 会话 id 由调用方（Unity/对话编排）指定，后端不再自行生成。
        existing = NpcDialogueSession.objects.select_for_update().select_related(
            "initiator", "target", "reservation"
        ).filter(conversation_id=conversation_id).first()
        if existing:
            participants = {str(existing.initiator.agent_id), str(existing.target.agent_id)}
            if existing.status == "ACTIVE" and participants == {npc_id, target_npc_id}:
                if topic.strip() and existing.topic != topic.strip():
                    existing.topic = topic.strip()
                    existing.save(update_fields=["topic", "updated_at"])
                return _result(True, ResultCode.DIALOGUE_STARTED, reused=True, **cls._summary(existing))
            # 历史已结束会话不可复活；会话 id 也不可被其他 NPC 对占用
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)

        # 已在与任何人进行 ACTIVE 对话的一方视为“正忙”。Unity 在调用这里前
        # 已把同一对 NPC 的并发首句归并到先创建的 conversation_id。
        active_session = NpcDialogueSession.objects.select_for_update().select_related(
            "initiator", "target", "reservation"
        ).filter(
            Q(initiator__agent_id=npc_id, status="ACTIVE")
            | Q(target__agent_id=npc_id, status="ACTIVE")
            | Q(initiator__agent_id=target_npc_id, status="ACTIVE")
            | Q(target__agent_id=target_npc_id, status="ACTIVE"),
        ).first()
        if active_session:
            busy_npc_id = npc_id if npc_id in {
                str(active_session.initiator.agent_id), str(active_session.target.agent_id)
            } else target_npc_id
            return _result(
                False, ResultCode.SOCIAL_TARGET_BUSY,
                busy_npc_id=busy_npc_id, **cls._summary(active_session)
            )

        runtimes = list(NpcRuntimeState.objects.select_for_update().select_related("npc").filter(
            npc__agent_id__in=[npc_id, target_npc_id], npc__is_test_only=False,
            npc__village_profile__is_active=True,
        ).order_by("npc__agent_id"))
        if {str(row.npc.agent_id) for row in runtimes} != {npc_id, target_npc_id}:
            return _result(False, ResultCode.DIALOGUE_PARTICIPANT_INVALID)
        # 对话只与移动/睡眠互斥；做菜、吃饭、工作等状态仍可边做边聊。
        unavailable = next(
            (row for row in runtimes if row.action_status in {"MOVING", "SLEEPING"}),
            None,
        )
        if unavailable:
            return _result(
                False, ResultCode.SOCIAL_TARGET_BUSY,
                busy_npc_id=str(unavailable.npc.agent_id)
            )
        start = time_state.world_minute
        expires = min(start + cls.MAX_DURATION_MINUTES, (start // MINUTES_PER_DAY + 1) * MINUTES_PER_DAY)
        for runtime in runtimes:
            if runtime.needs_world_minute < start:
                error = NeedsService.advance_base_locked(runtime, start)
                if error:
                    return _result(False, error)

        ok, code, reservation = ReservationService.acquire(
            action_kind="DIALOGUE", participant_ids=[npc_id, target_npc_id],
            world_minute=start, expires_world_minute=expires,
            location_code=runtimes[0].current_location_code,
        )
        if not ok:
            return _result(False, code)
        by_id = {str(row.npc.agent_id): row for row in runtimes}
        session = NpcDialogueSession.objects.create(
            conversation_id=conversation_id,
            initiator=by_id[npc_id].npc,
            target=by_id[target_npc_id].npc,
            reservation=reservation,
            topic=topic.strip(),
            started_world_minute=start,
            expires_world_minute=expires,
        )
        for runtime in runtimes:
            runtime.state_version += 1
            runtime.time_revision = time_state.time_revision
            runtime.save()
        return _result(True, ResultCode.DIALOGUE_STARTED, **cls._summary(session))

    @classmethod
    @transaction.atomic
    def speak(cls, conversation_id: str, npc_id: str, text: str, end_turn: bool = False) -> Dict[str, Any]:
        conversation_id = str(conversation_id or "")
        if not isinstance(text, str) or not text.strip() or len(text) > cls.MAX_TEXT_LENGTH or not isinstance(end_turn, bool):
            return _result(False, ResultCode.DIALOGUE_TEXT_INVALID)
        try:
            time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
            session = NpcDialogueSession.objects.select_for_update().select_related(
                "initiator", "target", "reservation"
            ).get(conversation_id=conversation_id)
        except GlobalGameTimeState.DoesNotExist:
            return _result(False, ResultCode.BACKEND_UNAVAILABLE)
        except NpcDialogueSession.DoesNotExist:
            return _result(False, ResultCode.DIALOGUE_NOT_FOUND)
        if session.status != "ACTIVE":
            return _result(False, ResultCode.DIALOGUE_NOT_ACTIVE, **cls._summary(session))
        if time_state.world_minute >= session.expires_world_minute:
            return cls._settle_locked(session, time_state, "EXPIRED")

        npc_id = str(npc_id or "")
        participant_ids = {str(session.initiator.agent_id), str(session.target.agent_id)}
        if npc_id not in participant_ids:
            return _result(False, ResultCode.DIALOGUE_PARTICIPANT_INVALID)
        if npc_id in (session.end_requests or []):
            return _result(False, ResultCode.DIALOGUE_ALREADY_ENDED_SELF)
        # No speaker-turn check: the original dialogue rule permits consecutive speech.
        session.messages = [*(session.messages or []), {
            "speaker_npc_id": npc_id,
            "text": text.strip(),
            "world_minute": time_state.world_minute,
            "end_turn": end_turn,
        }]
        if end_turn and npc_id not in (session.end_requests or []):
            session.end_requests = [*(session.end_requests or []), npc_id]
        session.save(update_fields=["messages", "end_requests", "updated_at"])
        if participant_ids.issubset(set(session.end_requests or [])):
            return cls._settle_locked(session, time_state, "BOTH_ENDED")
        return _result(True, ResultCode.DIALOGUE_SPOKEN, **cls._summary(session))

    @classmethod
    @transaction.atomic
    def complete(cls, conversation_id: str, npc_id: str, reason: str = "COMPLETED") -> Dict[str, Any]:
        conversation_id = str(conversation_id or "")
        if not isinstance(reason, str) or len(reason) > 64:
            return _result(False, ResultCode.INVALID_COMMAND_SCHEMA)
        try:
            time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
            session = NpcDialogueSession.objects.select_for_update().select_related(
                "initiator", "target", "reservation"
            ).get(conversation_id=conversation_id)
        except NpcDialogueSession.DoesNotExist:
            return _result(False, ResultCode.DIALOGUE_NOT_FOUND)
        except GlobalGameTimeState.DoesNotExist:
            return _result(False, ResultCode.BACKEND_UNAVAILABLE)
        if str(npc_id or "") not in {str(session.initiator.agent_id), str(session.target.agent_id)}:
            return _result(False, ResultCode.DIALOGUE_PARTICIPANT_INVALID)
        if session.status != "ACTIVE":
            return _result(True, ResultCode.COMMAND_REPLAYED, **cls._summary(session))
        return cls._settle_locked(session, time_state, reason or "COMPLETED")

    @classmethod
    def _settle_locked(cls, session, time_state, reason: str, force_invalid: bool = False) -> Dict[str, Any]:
        end = min(time_state.world_minute, session.expires_world_minute)
        runtimes = list(NpcRuntimeState.objects.select_for_update().select_related("npc").filter(
            npc__in=[session.initiator, session.target]
        ).order_by("npc__agent_id"))
        speakers = {str(message.get("speaker_npc_id")) for message in (session.messages or [])}
        participant_ids = {str(session.initiator.agent_id), str(session.target.agent_id)}
        valid = not force_invalid and participant_ids.issubset(speakers)
        message_minutes = [
            int(message["world_minute"])
            for message in (session.messages or [])
            if isinstance(message, dict) and isinstance(message.get("world_minute"), int)
        ]
        last_message_minute = max(message_minutes, default=session.started_world_minute)
        # A conversation is valid based on both sides speaking, while its need
        # reward stops at the last message (with a small floor for same-minute
        # exchanges). This prevents a stale ACTIVE row from farming SOCIALIZE.
        credited_end = min(
            end,
            max(session.started_world_minute + cls.MIN_CREDITED_DURATION_MINUTES, last_message_minute),
        )
        if credited_end < session.started_world_minute:
            credited_end = session.started_world_minute
        multiplier = None
        if valid:
            low, high = runtimes
            counter, _ = NpcSocialDailyCounter.objects.select_for_update().get_or_create(
                low_npc=low.npc, high_npc=high.npc, day_index=session.started_world_minute // MINUTES_PER_DAY
            )
            multiplier = 100 if counter.socialize_count == 0 else 50 if counter.socialize_count == 1 else 25
            counter.socialize_count += 1
            counter.save(update_fields=["socialize_count", "updated_at"])

        participant_needs = {}
        for runtime in runtimes:
            if runtime.needs_world_minute < credited_end:
                error = NeedsService.advance_base_locked(runtime, credited_end, finalize_days=False)
                if error:
                    return _result(False, error)
            if valid and credited_end > session.started_world_minute:
                error = NeedsService.apply_behavior_delta_locked(
                    runtime, session.started_world_minute, credited_end, "SOCIALIZE", multiplier
                )
                if error:
                    return _result(False, error)
            if runtime.needs_world_minute < time_state.world_minute:
                error = NeedsService.advance_base_locked(
                    runtime, time_state.world_minute, finalize_days=False
                )
                if error:
                    return _result(False, error)
            for day_index in range(
                    session.started_world_minute // MINUTES_PER_DAY,
                    time_state.world_minute // MINUTES_PER_DAY):
                NeedsService._finalize_day(runtime, day_index)
            runtime.state_version += 1
            runtime.time_revision = time_state.time_revision
            runtime.save()
            participant_needs[str(runtime.npc.agent_id)] = public_needs(runtime)

        # Keep the settlement as one auditable participant event and apply the
        # small symmetric relationship change through the existing reducer.
        from .phase4_social import reduce_relationship, _event
        event = _event(
            event_type="DIALOGUE_SETTLED",
            world_minute=end,
            time_revision=int(time_state.time_revision),
            request_id=None,
            command_id=None,
            actor=session.initiator,
            participants=sorted(participant_ids),
            location_code=session.reservation.location_code,
            visibility="PARTICIPANTS",
            public_data={
                "conversation_id": str(session.conversation_id),
                "valid": valid,
                "credited_duration_minutes": max(0, credited_end - session.started_world_minute),
                "hub_summary": "NPC dialogue settled.",
            },
            private_data={
                "message_count": len(session.messages or []),
                "social_multiplier_percent": multiplier,
            },
        )
        if valid:
            for from_npc_id, to_npc_id in (
                (session.initiator.agent_id, session.target.agent_id),
                (session.target.agent_id, session.initiator.agent_id),
            ):
                reduce_relationship(
                    event, from_npc_id, to_npc_id, "DIALOGUE",
                    {"affinity": 4, "trust": 4},
                )

        reservation = session.reservation
        expired = force_invalid or reason == "EXPIRED"
        reservation.status = "EXPIRED" if expired else "COMMITTED"
        reservation.released_world_minute = end
        reservation.active_lock_key = None
        reservation.save(update_fields=["status", "released_world_minute", "active_lock_key", "updated_at"])
        session.status = "EXPIRED" if expired else "COMPLETED"
        session.ended_world_minute = end
        session.end_reason = reason
        session.valid_for_social_needs = valid
        session.social_multiplier_percent = multiplier
        session.save()
        return _result(
            True, ResultCode.DIALOGUE_COMPLETED, **cls._summary(session),
            credited_end_world_minute=credited_end,
            credited_duration_minutes=max(0, credited_end - session.started_world_minute),
            world_event_id=str(event.event_id), participant_needs=participant_needs
        )

    @classmethod
    @transaction.atomic
    def expire_active(
        cls, world_minute: int, include_unexpired: bool = False, force_invalid: bool = False
    ) -> int:
        time_state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        filters = {"status": "ACTIVE"}
        if not include_unexpired:
            filters["expires_world_minute__lte"] = world_minute
        sessions = list(NpcDialogueSession.objects.select_for_update().select_related(
            "initiator", "target", "reservation"
        ).filter(**filters))
        if not sessions:
            return 0
        for session in sessions:
            cls._settle_locked(session, time_state, "EXPIRED", force_invalid=force_invalid)
        return len(sessions)

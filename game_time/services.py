"""Read-only helpers around the global time authority.

The sole production mutation path is Village runtime time CAS.  Calendar
fields are projections of ``world_minute``; this module has no calendar setter.
"""

from .models import GlobalGameTimeState


class TimeAuthorityService:
    @staticmethod
    def snapshot(state=None):
        state = state or GlobalGameTimeState.get_instance()
        expected = GlobalGameTimeState.world_minute_from_calendar(
            state.game_year, state.game_month, state.game_day,
            state.game_hour, state.game_minute,
        )
        if state.world_minute != expected:
            raise RuntimeError("global time authority is inconsistent")
        return state

    @staticmethod
    def legacy_mutation_rejected():
        return "legacy time mutation is disabled; use Village runtime time CAS"

"""Protect the village batch contract while legacy non-village flows remain."""
from rest_framework.response import Response


def legacy_production_guard():
    from .models import VillageSimulationConfig
    if VillageSimulationConfig.objects.filter(config_key="default", village_economy_enabled=True).exists():
        return Response({"success": False, "result_code": "VILLAGE_ACTION_REQUIRED",
                         "message": "请使用村庄烹饪或送评面板，按真实批次结算。"}, status=409)
    return None


def needs_batch_safe_stall(slot):
    """G4 will replace this temporary guard with lossless menu transfers."""
    return bool(slot and (slot.expires_world_minute is not None or slot.freshness_unknown
                          or slot.dish_metadata))

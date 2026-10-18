"""Frozen G6 content identities; no queries or initialization on import."""
PUMPKIN, PUMPKIN_SEED = 1001, 29307
PROFESSIONAL_MUSHROOM, MOUNTAIN_HERB, FOREST_BERRY = 29308, 29309, 29310
PROFESSIONAL_WOOD, PREY_BOAR = 29311, 29108
PROFESSIONAL_EXPORT_PRICES = {PUMPKIN: 8, PROFESSIONAL_MUSHROOM: 10, MOUNTAIN_HERB: 10,
                              FOREST_BERRY: 10, PROFESSIONAL_WOOD: 12}
PROFESSIONAL_IMPORT_LIMITS = {PUMPKIN_SEED: 30}
PROFESSIONAL_COLLECT_TYPES = {
    "采集深林菌": dict(item_id=PROFESSIONAL_MUSHROOM, title="深林菌", code="DEEP_MUSHROOM"),
    "采集山香草": dict(item_id=MOUNTAIN_HERB, title="山香草", code="MOUNTAIN_HERB"),
    "采集林莓": dict(item_id=FOREST_BERRY, title="林莓", code="FOREST_BERRY"),
}
T3_TOOLS = {
    4120: dict(name="专业采集包", capability="targeted_gathering", skill="gathering", uses=100),
    4121: dict(name="专业农具", capability="farming", skill="farming", uses=100),
    4122: dict(name="专业斧", capability="forestry", skill="forestry", uses=100),
    4123: dict(name="专业炊具", capability="cooking", skill="cooking", uses=100),
    4124: dict(name="专业长矛", capability="hunting_spear", skill="hunting", uses=180, weapon=0),
    4125: dict(name="专业弓", capability="hunting_bow", skill="hunting", uses=180, weapon=2),
}
T3_UPGRADES = {4102: (4120, 4), 4103: (4121, 4), 4105: (4122, 4),
               4107: (4123, 4), 4111: (4124, 4), 4113: (4125, 4)}
PROFESSIONAL_RECIPES = {
    29251: dict(item_id=29206, name="南瓜菌菇汤", ingredients={1001: 1, 29308: 2, 1049: 1}, seconds=300),
    29252: dict(item_id=29207, name="山香草鹿肉汤", ingredients={29107: 2, 29309: 1, 1049: 1}, seconds=300),
    29253: dict(item_id=29208, name="林莓玉米羹", ingredients={1030: 2, 29310: 1, 1049: 1}, seconds=240),
}


def batch_evidence(slot):
    """Immutable actual output facts, shared by production and mastery."""
    return dict(instance_id=str(slot.instance_id), item_id=slot.item_id, quantity=slot.item_amount,
        quality=slot.quality, produced_world_minute=slot.produced_world_minute,
        expires_world_minute=slot.expires_world_minute,
        source_request_id=str(slot.source_request_id) if slot.source_request_id else None)

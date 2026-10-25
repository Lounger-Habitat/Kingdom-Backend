"""Plant identities and authored offsets from existing village location anchors."""
import uuid

PLANTS = {
    "WILD_CARROT": dict(item_id=1025, level=1, location="FOREST_EDGE", model="carrot", seconds=4,
                        quantity=3, action_cost=5, quality=3, regrow_minutes=480, collect_type="采集食材"),
    "WILD_POTATO": dict(item_id=1042, level=1, location="FOREST_EDGE", model="potato", seconds=4,
                        quantity=3, action_cost=5, quality=3, regrow_minutes=480, collect_type="采集食材"),
    "OYSTER": dict(item_id=1035, level=1, location="FOREST_EDGE", model="oyster", seconds=4,
                   quantity=3, action_cost=5, quality=3, regrow_minutes=360, collect_type="采集食材"),
    "ENOKI": dict(item_id=1037, level=2, location="FOREST_EDGE", model="enoki", seconds=5,
                  quantity=3, action_cost=5, quality=3, regrow_minutes=480, collect_type="采集食材"),
    "UNDERGROWTH": dict(item_id=1036, level=4, location="FOREST_UNDERGROWTH", model="wood_ear", seconds=6,
                        quantity=4, action_cost=10, quality=3, regrow_minutes=720, collect_type="采集林下食材"),
    "DEEP_MUSHROOM": dict(item_id=29308, level=7, location="DEEP_FOREST", model="chanterelle", seconds=7,
                          quantity=2, action_cost=20, quality=4, regrow_minutes=1440, collect_type="采集深林菌"),
    "MOUNTAIN_HERB": dict(item_id=29309, level=7, location="DEEP_FOREST", model="sage", seconds=7,
                         quantity=2, action_cost=20, quality=4, regrow_minutes=1440, collect_type="采集山香草"),
    "FOREST_BERRY": dict(item_id=29310, level=7, location="DEEP_FOREST", model="blueberry", seconds=6,
                        quantity=2, action_cost=20, quality=4, regrow_minutes=1440, collect_type="采集林莓"),
}

# Stable identities survive scene reloads. Positions are resolved on the Env, never supplied by a player.
NODES = {}
for species, offsets in {
    "WILD_CARROT": ((-5, 0), (-5, 5), (2, 7)),
    "WILD_POTATO": ((0, -6), (5, -6), (-4, -6)),
    "OYSTER": ((1, 1), (-2, 1), (3, -1), (-3, -2), (1, -4), (4, 3)),
    "ENOKI": ((-4, 3), (4, -4), (-1, 5)),
    "UNDERGROWTH": ((2, 1), (-1, -2), (1, -4), (-1, -5), (-4, -3), (-4, -5)),
    "DEEP_MUSHROOM": ((2, 1), (-3, -2), (4, 5)),
    "MOUNTAIN_HERB": ((-4, 3), (1, 6), (5, -3)),
    "FOREST_BERRY": ((-5, -4), (5, 2), (-2, 7)),
}.items():
    for index, offset in enumerate(offsets):
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kingdom:plant:v1:{species}:{index}"))
        NODES[identity] = dict(species=species, offset=list(offset))

# Beginner forage behind Bill's farmhouse, relative to LOC_BILL_FARM.
# Keep this namespace independent so adding these plants never renumbers the forest.
for index, (species, offset) in enumerate((
    ("OYSTER", (-4.57, 28.62)),
    ("WILD_CARROT", (-6.35, 28.12)),
    ("WILD_POTATO", (-4.65, 26.32)),
    ("OYSTER", (-7.05, 25.82)),
    ("WILD_CARROT", (-4.57, 24.02)),
    ("OYSTER", (-6.55, 23.22)),
    ("WILD_POTATO", (-4.55, 21.72)),
    ("OYSTER", (-8.05, 22.02)),
)):
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kingdom:plant:v1:bill-house:{index}"))
    NODES[identity] = dict(species=species, offset=list(offset), location_code="BILL_FARM")

TIME_REDUCTION = {0: 0, 1: .15, 2: .30, 3: .45}
QUALITY_BONUS_CHANCE = {0: 0, 1: 0, 2: .10, 3: .20}
ACTIONS = {"plant_start": {"node_id"}, "plant_cancel": {"attempt_id"},
           "equip_gathering_tool": {"instance_id"}}

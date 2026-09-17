"""Small receipt helpers; never infer settlement facts from request parameters."""


def add_item_names(value):
    from items.models import Item
    rows = []

    def collect(node):
        if isinstance(node, dict):
            item_id = node.get("item_id")
            if type(item_id) is int and not (node.get("item_name") or node.get("name")):
                rows.append(node)
            for child in node.values():
                collect(child)
        elif isinstance(node, list):
            for child in node:
                collect(child)

    collect(value)
    if rows:
        names = dict(Item.objects.filter(item_id__in={r["item_id"] for r in rows}).values_list("item_id", "item_name"))
        for row in rows:
            if row["item_id"] in names:
                row["item_name"] = names[row["item_id"]]
    return value


def received_batch(slot, amount):
    data = {"instance_id": str(slot.instance_id), "item_id": slot.item_id, "amount": amount,
            "expires_world_minute": slot.expires_world_minute,
            "freshness_unknown": slot.freshness_unknown, "remaining_uses": slot.remaining_uses}
    from items.models import Item, ItemType
    item = Item.objects.filter(item_id=slot.item_id).first()
    if item:
        data["item_name"] = item.item_name
        if item.item_type == ItemType.FOOD:
            data["food_instance_id"] = str(slot.instance_id)
    return data

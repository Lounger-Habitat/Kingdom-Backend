"""Small receipt helpers; never infer settlement facts from request parameters."""


def add_item_names(value):
    from items.models import Item, ItemType
    rows = []
    references = []
    fields = {"item_id": "item_name", "itemId": "itemName"}
    for prefix in ("output", "seed", "harvest", "required", "unavailable", "tool", "prey", "meal", "supply", "limit"):
        fields[prefix + "_item_id"] = prefix + "_item_name"

    def supplied_name(node, item_id):
        for key in ("item_name", "itemName", "name"):
            name = node.get(key)
            if isinstance(name, str) and name.strip() and name != str(item_id):
                return name
        return None

    def collect(node, parent_item=None):
        if isinstance(node, dict):
            item_id = node.get("item_id", node.get("itemId"))
            # A batch inside a named item row is the same actual item. No request
            # parameters or live inventory lookup are used to guess its identity.
            if item_id is None and parent_item and (node.get("instance_id") or node.get("food_instance_id")):
                item_id, name = parent_item
                node["item_id"] = item_id
                if name and not supplied_name(node, item_id):
                    node["item_name"] = name
            current_item = None
            if type(item_id) is int and item_id > 0:
                rows.append((node, item_id))
                current_item = (item_id, supplied_name(node, item_id))
            for id_field, name_field in fields.items():
                identity = node.get(id_field)
                if type(identity) is int and identity > 0:
                    references.append((node, id_field, name_field, identity))
            for child in node.values():
                collect(child, current_item)
        elif isinstance(node, list):
            for child in node:
                collect(child, parent_item)

    collect(value)
    if references:
        items = {item.item_id: item for item in Item.objects.filter(item_id__in={r[3] for r in references}).only("item_id", "item_name", "item_type")}
        for row, id_field, name_field, item_id in references:
            existing = supplied_name(row, item_id) if id_field in {"item_id", "itemId"} else row.get(name_field)
            item = items.get(item_id)
            if not existing and item and item.item_name.strip():
                row[name_field] = item.item_name
        for row, item_id in rows:
            item = items.get(item_id)
            if item and item.item_type == ItemType.FOOD and row.get("instance_id") and not row.get("food_instance_id"):
                row["food_instance_id"] = row["instance_id"]
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

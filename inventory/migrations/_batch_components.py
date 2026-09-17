"""Frozen data conversion for the phase-two migrations; no runtime model imports."""

FIELDS = {
    "Usage": {"remaining": ("remaining_uses", None)},
    "Dish": {"ingredients": ("ingredient", ""), "cook_time": ("cook_time", 0), "metadata": ("dish_metadata", {})},
    "Review": {"rated": ("rated", False), "price": ("rating_price", -1), "score": ("overall_score", 0), "comment": ("review_comment", "")},
}


def migrate_owner(apps, schema_editor, app, name, reverse=False):
    alias = schema_editor.connection.alias
    owner = apps.get_model(app, name)
    details = {suffix: apps.get_model(app, name + suffix) for suffix in FIELDS}
    store = name == "VillageStoreInventorySlot"
    market = name == "MarketListing"
    rows = owner.objects.using(alias).order_by("pk")
    if reverse:
        rows = rows.select_related("usage", "dish", "review")
    buffers = {suffix: [] for suffix in FIELDS}
    for row in rows.iterator(chunk_size=500):
        attrs = dict(row.common_attributes or {}) if store else None
        if reverse:
            update = {}
            for suffix, fields in FIELDS.items():
                detail = getattr(row, suffix.lower(), None)
                for field, (legacy, default) in fields.items():
                    value = getattr(detail, field, default)
                    if store and legacy != "remaining_uses":
                        attrs[legacy] = value
                    elif market and legacy == "dish_metadata":
                        snapshot = dict(row.item_snapshot or {})
                        if value or "dishMetadata" in snapshot:
                            snapshot["dishMetadata"] = value
                        update["item_snapshot"] = snapshot
                    else:
                        update[legacy] = value
            if store:
                attrs["dish_quality"] = row.quality
                update["common_attributes"] = attrs
            owner.objects.using(alias).filter(pk=row.pk).update(**update)
            continue
        for suffix, fields in FIELDS.items():
            values = {}
            for field, (legacy, default) in fields.items():
                if store and legacy != "remaining_uses":
                    values[field] = attrs.pop(legacy, default)
                elif market and legacy == "dish_metadata":
                    values[field] = (row.item_snapshot or {}).get("dishMetadata") or {}
                else:
                    values[field] = getattr(row, legacy)
            if any(value != fields[field][1] for field, value in values.items()):
                buffers[suffix].append(details[suffix](owner_id=row.pk, **values))
                if len(buffers[suffix]) >= 500:
                    details[suffix].objects.using(alias).bulk_create(buffers[suffix], batch_size=500)
                    buffers[suffix].clear()
        if store:
            quality = attrs.pop("dish_quality", 3)
            owner.objects.using(alias).filter(pk=row.pk).update(common_attributes=attrs, quality=quality)
    for suffix, buffer in buffers.items():
        if buffer:
            details[suffix].objects.using(alias).bulk_create(buffer, batch_size=500)

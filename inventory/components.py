"""Typed instance storage shared by inventory, stores and marketplace.

The flat Python properties are a compatibility boundary, not database columns.
Use related paths in ORM queries (e.g. usage__remaining), and save() for component
writes. Parent and component changes always commit together. Each owner has its
own one-to-one detail tables: no generic foreign keys or shared mutable instances.
"""
from copy import deepcopy

from django.core.exceptions import ObjectDoesNotExist
from django.db import models, router, transaction


# legacy name: (relation, typed field, legacy default)
COMPONENT_FIELDS = {
    "remaining_uses": ("usage", "remaining", None),
    "ingredient": ("dish", "ingredients", ""),
    "cook_time": ("dish", "cook_time", 0),
    "dish_metadata": ("dish", "metadata", {}),
    "rated": ("review", "rated", False),
    "rating_price": ("review", "price", -1),
    "overall_score": ("review", "score", 0),
    "review_comment": ("review", "comment", ""),
}

# Copy by value when ownership changes; never share a mutable detail row. Batch
# identity/quantity and presentation location are deliberately controlled by caller.
BATCH_COPY_FIELDS = (*COMPONENT_FIELDS, "quality", "produced_world_minute",
                     "expires_world_minute", "source_request_id", "freshness_unknown")


def copy_batch_attributes(source, target):
    for name in BATCH_COPY_FIELDS:
        setattr(target, name, deepcopy(getattr(source, name)))


def same_batch_attributes(left, right):
    return all(getattr(left, name) == getattr(right, name) for name in BATCH_COPY_FIELDS)


class UsageFields(models.Model):
    # NULL = unknown/not recorded, -1 = unlimited; preserve invalid historical
    # values for the existing fail-closed durability validation, never repair here.
    remaining = models.IntegerField(null=True, blank=True)

    class Meta:
        abstract = True


class DishFields(models.Model):
    ingredients = models.CharField(max_length=200, default="", blank=True)
    cook_time = models.IntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True


class ReviewFields(models.Model):
    rated = models.BooleanField(default=False)
    price = models.IntegerField(default=-1)
    score = models.FloatField(default=0)
    comment = models.TextField(default="", blank=True)

    class Meta:
        abstract = True


class BatchQuerySet(models.QuerySet):
    def select_for_update(self, nowait=False, skip_locked=False, of=(), no_key=False):
        # PostgreSQL cannot FOR UPDATE the nullable side of detail outer joins.
        # All component writes lock this parent first, so locking self is enough.
        return super().select_for_update(nowait, skip_locked, of or ("self",), no_key)

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        if any(obj._component_changes() or getattr(obj, "item_snapshot", None) for obj in objs):
            raise ValueError("Component-bearing batches require save(), not bulk_create().")
        return super().bulk_create(objs, *args, **kwargs)


class BatchManager(models.Manager.from_queryset(BatchQuerySet)):
    def get_queryset(self):
        return super().get_queryset().select_related("usage", "dish", "review")


def component_property(name):
    relation, field, default = COMPONENT_FIELDS[name]

    def get(instance):
        pending = instance.__dict__.setdefault("_component_values", {})
        if name not in pending:
            try:
                value = getattr(getattr(instance, relation), field)
            except ObjectDoesNotExist:
                value = default
            pending[name] = deepcopy(value)
            instance.__dict__.setdefault("_component_original", {})[name] = deepcopy(value)
        return pending[name]

    def set_value(instance, value):
        instance.__dict__.setdefault("_component_values", {})[name] = deepcopy(value)

    return property(get, set_value)


class BatchComponents(models.Model):
    objects = BatchManager()

    @property
    def dish_quality(self):
        """Legacy name; quality is a common batch field, not a dish component."""
        return self.quality

    @dish_quality.setter
    def dish_quality(self, value):
        self.quality = value

    remaining_uses = component_property("remaining_uses")
    ingredient = component_property("ingredient")
    cook_time = component_property("cook_time")
    dish_metadata = component_property("dish_metadata")
    rated = component_property("rated")
    rating_price = component_property("rating_price")
    overall_score = component_property("overall_score")
    review_comment = component_property("review_comment")

    class Meta:
        abstract = True

    def _component_changes(self):
        original = self.__dict__.get("_component_original", {})
        return {name: value for name, value in self.__dict__.get("_component_values", {}).items()
                if name not in original or value != original[name]}

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = {"quality" if name == "dish_quality" else name for name in update_fields}
            kwargs["update_fields"] = update_fields
        if update_fields is not None and not update_fields:
            return
        changes = self._component_changes()
        if update_fields is not None:
            # Explicit update_fields can save a related object changed directly.
            changes = {name: getattr(self, name) for name in update_fields if name in COMPONENT_FIELDS}
            kwargs["update_fields"] = set(update_fields) - COMPONENT_FIELDS.keys()
        if not changes:
            return super().save(*args, **kwargs)
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            if not self._state.adding:
                # Also serialize detail-only saves, which do not UPDATE the parent.
                type(self)._base_manager.using(using).select_for_update().get(pk=self.pk)
            if update_fields is None or kwargs["update_fields"]:
                super().save(*args, **kwargs)
            for relation in ("usage", "dish", "review"):
                values = {COMPONENT_FIELDS[name][1]: value for name, value in changes.items()
                          if COMPONENT_FIELDS[name][0] == relation}
                if not values:
                    continue
                detail_model = self._meta.get_field(relation).related_model
                # Do not allocate a detail row for default-only ordinary items.
                defaults = {field: default for rel, field, default in COMPONENT_FIELDS.values() if rel == relation}
                rows = detail_model.objects.using(using).filter(owner_id=self.pk)
                if all(value == defaults[field] for field, value in values.items()):
                    rows.update(**values)
                else:
                    detail_model.objects.using(using).update_or_create(owner_id=self.pk, defaults=values)
                # Remove now-empty optional rows (e.g. slot cleared or reused).
                rows.filter(**defaults).delete()
                self._state.fields_cache.pop(relation, None)
        original = self.__dict__.setdefault("_component_original", {})
        original.update(deepcopy(changes))

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        # Components are not concrete parent fields. Refresh their cached values
        # as well; otherwise refresh_from_db() could return stale usage/reviews.
        if fields is not None:
            fields = {"quality" if name == "dish_quality" else name for name in fields}
        component_names = set(COMPONENT_FIELDS) if fields is None else set(fields) & COMPONENT_FIELDS.keys()
        if fields is not None:
            fields = set(fields) - COMPONENT_FIELDS.keys()
        if fields is None or fields:
            super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        for name in component_names:
            self.__dict__.get("_component_values", {}).pop(name, None)
            self.__dict__.get("_component_original", {}).pop(name, None)
            self._state.fields_cache.pop(COMPONENT_FIELDS[name][0], None)

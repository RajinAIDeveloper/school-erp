"""Explicitly configured CRUD screens for school master data, not financial events."""
from django.forms import modelform_factory
from django.urls import path

from .forms import SchoolModelForm
from .generic import ERPCreateView, ERPListView, ERPUpdateView


def crud(model, namespace, key, fields, columns, *, prefix=None, form=None, search=(), actions=()):
    prefix = (key.replace("_", "-") + "/") if prefix is None else prefix
    form = form or modelform_factory(model, form=SchoolModelForm, fields=fields)
    label = model._meta.verbose_name_plural.title()
    base = f"{model._meta.app_label}."
    stem = model._meta.model_name
    list_name = key + "_list" if key else "list"
    create_name = key + "_create" if key else "create"
    update_name = key + "_update" if key else "update"
    common = dict(model=model, page_title=label)
    listing = type(stem + "List", (ERPListView,), dict(
        **common, permission_required=base + "view_" + stem, columns=columns,
        search_fields=search, create_url_name=namespace + ":" + create_name,
        update_url_name=namespace + ":" + update_name, extra_actions=actions,
    ))
    attrs = dict(**common, form_class=form, success_url_name=namespace + ":" + list_name)
    create = type(stem + "Create", (ERPCreateView,), dict(**attrs, permission_required=base + "add_" + stem))
    update = type(stem + "Update", (ERPUpdateView,), dict(**attrs, permission_required=base + "change_" + stem))
    return [path(prefix, listing.as_view(), name=list_name),
            path(prefix + "new/", create.as_view(), name=create_name),
            path(prefix + "<int:pk>/edit/", update.as_view(), name=update_name)]

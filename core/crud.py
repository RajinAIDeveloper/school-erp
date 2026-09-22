"""Explicitly configured CRUD screens for school master data, not financial events."""

from django.forms import modelform_factory
from django.urls import path

from .forms import SchoolModelForm
from .generic import ERPCreateView, ERPDeleteView, ERPListView, ERPUpdateView


def crud(
    model,
    namespace,
    key,
    fields,
    columns,
    *,
    prefix=None,
    form=None,
    search=(),
    actions=(),
    list_permission=None,
    scope=None,
    deletable=False,
    filters=(),
    row_forms=(),
):
    """
    Build list/create/update (and optionally delete) screens for one model.

    actions          header links as ("Label", "url_name") or ("Label", "url_name", "app.perm")
    list_permission  override the default view_<model> permission, e.g. to keep a management
                     list away from roles that only hold the read permission for a portal screen
    scope            callable(view, queryset) narrowing records beyond the tenant
    """
    prefix = (key.replace("_", "-") + "/") if prefix is None else prefix
    form = form or modelform_factory(model, form=SchoolModelForm, fields=fields)
    label = model._meta.verbose_name_plural.title()
    base = f"{model._meta.app_label}."
    stem = model._meta.model_name
    list_name = key + "_list" if key else "list"
    create_name = key + "_create" if key else "create"
    update_name = key + "_update" if key else "update"
    delete_name = key + "_delete" if key else "delete"
    common = dict(model=model, page_title=label)
    listing_attrs = dict(
        **common,
        permission_required=list_permission or base + "view_" + stem,
        columns=columns,
        search_fields=search,
        create_url_name=namespace + ":" + create_name,
        update_url_name=namespace + ":" + update_name,
        extra_actions=actions,
        filters=filters,
        row_forms=row_forms,
    )
    if deletable:
        listing_attrs["delete_url_name"] = namespace + ":" + delete_name
    if scope is not None:
        listing_attrs["scope_queryset"] = lambda self, qs, _scope=scope: _scope(self, qs)
    listing = type(stem + "List", (ERPListView,), listing_attrs)
    attrs = dict(**common, form_class=form, success_url_name=namespace + ":" + list_name)
    create = type(stem + "Create", (ERPCreateView,), dict(**attrs, permission_required=base + "add_" + stem))
    update = type(stem + "Update", (ERPUpdateView,), dict(**attrs, permission_required=base + "change_" + stem))
    routes = [
        path(prefix, listing.as_view(), name=list_name),
        path(prefix + "new/", create.as_view(), name=create_name),
        path(prefix + "<int:pk>/edit/", update.as_view(), name=update_name),
    ]
    if deletable:
        remove = type(
            stem + "Delete",
            (ERPDeleteView,),
            dict(**common, permission_required=base + "delete_" + stem, success_url_name=namespace + ":" + list_name),
        )
        routes.append(path(prefix + "<int:pk>/delete/", remove.as_view(), name=delete_name))
    return routes

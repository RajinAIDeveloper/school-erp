"""
Generic CRUD views: a module defines columns + form + url names and gets a fully
working Tailwind list/create/update/delete UI, already tenant-scoped.
"""

from django.contrib import messages
from django.urls import reverse
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .mixins import SchoolScopedMixin


class ERPListView(SchoolScopedMixin, ListView):
    template_name = "generic/list.html"
    paginate_by = 25
    columns = ()  # [("Label", "attr.path", "kind"), ...]
    create_url_name = None
    update_url_name = None
    delete_url_name = None
    detail_url_name = None
    extra_actions = ()  # [("Label", "url_name")] or [("Label", "url_name", "app.perm")]
    row_forms = ()  # [("Label", "url_name", "app.perm", "confirm text")] POST buttons per row
    filters = ()  # [("param", "Label", [(value, label), ...]), ...]
    empty_message = "No records found."

    def visible_actions(self):
        """Header links the current user may actually open, so no action ever leads to a 403."""
        visible = []
        for action in self.extra_actions:
            label, name = action[0], action[1]
            permission = action[2] if len(action) > 2 else None
            if permission is None or self.request.user.has_perm(permission):
                visible.append((label, reverse(name)))
        return visible

    def get_filter_choices(self):
        return self.filters

    def apply_filters(self, qs):
        for param, _label, _choices in self.get_filter_choices():
            value = self.request.GET.get(param)
            if value:
                qs = qs.filter(**{param: value})
        return qs

    def get_queryset(self):
        return self.apply_filters(super().get_queryset())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        model = self.model
        app = model._meta.app_label
        stem = model._meta.model_name
        ctx.update(
            {
                "columns": self.columns,
                "create_url": reverse(self.create_url_name)
                if self.create_url_name and self.request.user.has_perm(f"{app}.add_{stem}")
                else None,
                "update_url_name": self.update_url_name if self.request.user.has_perm(f"{app}.change_{stem}") else None,
                "delete_url_name": self.delete_url_name if self.request.user.has_perm(f"{app}.delete_{stem}") else None,
                "detail_url_name": self.detail_url_name,
                "extra_actions": self.visible_actions(),
                "row_forms": [
                    (label, name, confirm)
                    for label, name, permission, confirm in self.row_forms
                    if self.request.user.has_perm(permission)
                ],
                "filters": self.get_filter_choices(),
                "empty_message": self.empty_message,
                "has_search": bool(self.search_fields),
            }
        )
        return ctx


class ERPFormMixin(SchoolScopedMixin):
    template_name = "generic/form.html"
    success_url_name = None
    cancel_url_name = None

    def form_valid(self, form):
        from django.core.exceptions import ValidationError
        from django.db import IntegrityError, transaction

        from .models import audit

        try:
            with transaction.atomic():
                response = super().form_valid(form)
                audit(self.request, "record.saved", self.object)
                return response
        except ValidationError as exc:
            form.add_error(None, exc)
        except IntegrityError:
            form.add_error(None, "A record with these unique values already exists. Reload and check your entries.")
        return self.form_invalid(form)

    def get_success_url(self):
        if self.success_url_name:
            return reverse(self.success_url_name)
        return super().get_success_url()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["cancel_url"] = (
            reverse(self.cancel_url_name or self.success_url_name)
            if (self.cancel_url_name or self.success_url_name)
            else None
        )
        return ctx


class ERPCreateView(ERPFormMixin, CreateView):
    success_message = "Created successfully."


class ERPUpdateView(ERPFormMixin, UpdateView):
    success_message = "Saved successfully."


class ERPDeleteView(SchoolScopedMixin, DeleteView):
    template_name = "generic/confirm_delete.html"
    success_url_name = None

    def get_success_url(self):
        return reverse(self.success_url_name)

    def form_valid(self, form):
        try:
            response = super().form_valid(form)
        except Exception as exc:  # ProtectedError etc.
            messages.error(self.request, f"Cannot delete: {exc}")
            return self.get(self.request)
        messages.success(self.request, "Deleted.")
        return response

"""
Reusable view mixins. All ERP views use SchoolScopedMixin so that every query is
automatically restricted to request.school - tenant isolation can never be forgotten.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Q


class ERPPermissionMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Login + Django permission check that raises 403 for logged-in users without access."""

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            raise PermissionDenied("You do not have permission to access this page.")
        return super().handle_no_permission()


class SchoolScopedMixin(ERPPermissionMixin):
    """
    For CBVs on SchoolScopedModel subclasses:
    - get_queryset() is filtered to request.school
    - forms receive a `school` kwarg so FK choices are limited to the tenant
    - on create, instance.school is set automatically
    """

    search_fields = ()
    select_related = ()
    page_title = ""
    success_message = ""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and (getattr(request, "school", None) is None or not request.school.is_active):
            raise PermissionDenied("Your account is not attached to a school. Ask an administrator.")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset().filter(school=self.request.school))
        if self.select_related:
            qs = qs.select_related(*self.select_related)
        q = self.request.GET.get("q", "").strip()
        if q and self.search_fields:
            cond = Q()
            for f in self.search_fields:
                cond |= Q(**{f"{f}__icontains": q})
            qs = qs.filter(cond)
        return qs

    def scope_queryset(self, qs):
        """Override to narrow records beyond the tenant, e.g. to a teacher's own sections."""
        return qs

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["school"] = self.request.school
        return kwargs

    def form_valid(self, form):
        if hasattr(form.instance, "school_id") and not form.instance.school_id:
            form.instance.school = self.request.school
        response = super().form_valid(form)
        if self.success_message:
            messages.success(self.request, self.success_message)
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault("page_title", self.page_title)
        ctx["q"] = self.request.GET.get("q", "")
        return ctx

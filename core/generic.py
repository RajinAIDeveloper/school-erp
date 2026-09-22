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
    columns = ()              # [("Label", "attr.path", "kind"), ...]
    create_url_name = None
    update_url_name = None
    delete_url_name = None
    detail_url_name = None
    extra_actions = ()        # [("Label", "url_name"), ...] shown in header
    filters = ()              # [("param", "Label", [(value, label), ...]), ...]
    empty_message = "No records found."

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
        ctx.update({
            "columns": self.columns,
            "create_url": reverse(self.create_url_name) if self.create_url_name else None,
            "update_url_name": self.update_url_name,
            "delete_url_name": self.delete_url_name,
            "detail_url_name": self.detail_url_name,
            "extra_actions": [(label, reverse(name)) for label, name in self.extra_actions],
            "filters": self.get_filter_choices(),
            "empty_message": self.empty_message,
            "has_search": bool(self.search_fields),
        })
        return ctx


class ERPFormMixin(SchoolScopedMixin):
    template_name = "generic/form.html"
    success_url_name = None
    cancel_url_name = None

    def form_valid(self, form):
        from django.core.exceptions import ValidationError
        from django.db import transaction, IntegrityError
        from .models import audit
        obj = form.instance
        try:
            with transaction.atomic():
                if obj._meta.label_lower == "examinations.exam" and obj.pk:
                    from examinations.models import Exam
                    old = Exam.objects.select_for_update().get(pk=obj.pk)
                    if old.publication_version:
                        raise ValidationError("Published exam configuration is locked.")
                if obj._meta.label_lower == "examinations.examschedule":
                    from examinations.models import Exam, ExamSchedule
                    ids = [obj.exam_id]
                    if obj.pk:
                        ids.append(ExamSchedule.objects.get(pk=obj.pk).exam_id)
                    if any(e.publication_version for e in Exam.objects.select_for_update().filter(pk__in=ids)):
                        raise ValidationError("Schedules of published exams cannot be changed.")
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
        ctx["cancel_url"] = reverse(self.cancel_url_name or self.success_url_name) if (self.cancel_url_name or self.success_url_name) else None
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

from django.contrib import messages
from django.shortcuts import render, redirect
from django.core.exceptions import PermissionDenied
from core.access import require_permission
from core.generic import ERPCreateView, ERPUpdateView, ERPListView
from core.models import audit
from .models import User
from .forms import UserForm, ProfileForm

class UserListView(ERPListView):
    model = User
    permission_required = "users.view_user"
    page_title = "User management"
    columns = (("Username","username"),("Name","get_full_name"),("Roles","role_names"),("Active","is_active","bool"))
    search_fields = ("username","first_name","last_name")
    create_url_name = "users:create"
    update_url_name = "users:update"

class UserCreateView(ERPCreateView):
    model = User
    form_class = UserForm
    permission_required = "users.add_user"
    page_title = "Create user"
    success_url_name = "users:list"
    def form_valid(self,form):
        response = super().form_valid(form)
        audit(self.request,"user.created",self.object)
        return response

class UserUpdateView(ERPUpdateView):
    model = User
    form_class = UserForm
    permission_required = "users.change_user"
    page_title = "Edit user"
    success_url_name = "users:list"
    def get_queryset(self):
        qs = super().get_queryset()
        return qs if self.request.user.is_superuser else qs.filter(is_superuser=False)
    def form_valid(self,form):
        if form.instance.pk == self.request.user.pk and not form.cleaned_data["is_active"]:
            form.add_error("is_active","You cannot deactivate your own account.")
            return self.form_invalid(form)
        response = super().form_valid(form)
        audit(self.request,"user.updated",self.object,"Roles/profile/password updated.")
        return response

@require_permission(None)
def profile(request):
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request,"Profile saved.")
        return redirect("users:profile")
    return render(request,"generic/form.html",{"form":form,"page_title":"My profile"})

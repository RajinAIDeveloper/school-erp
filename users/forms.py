from django import forms
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from core.forms import SchoolModelForm, TailwindFormMixin
from core.roles import ALL_ROLES
from .models import User

class UserForm(SchoolModelForm):
    password = forms.CharField(widget=forms.PasswordInput, required=False, help_text="Required for new accounts; leave blank to keep an existing password.")
    roles = forms.ModelMultipleChoiceField(queryset=Group.objects.filter(name__in=ALL_ROLES), required=False)
    class Meta:
        model = User
        fields = ["username","first_name","last_name","email","phone","is_active"]
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        if self.instance.pk:
            self.fields["roles"].initial = self.instance.groups.all()
    def clean(self):
        data = super().clean()
        password = data.get("password")
        if not self.instance.pk and not password:
            self.add_error("password","Set a password for the new user.")
        if password:
            validate_password(password, self.instance)
        return data
    def save(self,commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("password"):
            obj.set_password(self.cleaned_data["password"])
        if commit:
            obj.save()
            obj.groups.set(self.cleaned_data["roles"])
        return obj

class ProfileForm(TailwindFormMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name","last_name","email","phone","avatar"]

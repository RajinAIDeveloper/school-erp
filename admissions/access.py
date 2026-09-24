"""
Who may reach the admission office's screens.

Every one sits behind the module: a school the platform administrator has not given online
admissions gets "not found" on all of them, whoever asks, before any permission is looked at.
"""

from django.contrib.auth.decorators import login_required

from core.access import require_permission
from core.modules import require_module


def admissions_view(permission, *, also=""):
    def decorate(view):
        narrowing = f"{also}; admissions module only" if also else "admissions module only"
        guarded = require_permission(permission, also=narrowing)(view)
        return login_required(require_module("admissions")(guarded))

    return decorate

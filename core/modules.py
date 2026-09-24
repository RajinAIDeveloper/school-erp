"""
Modules the platform administrator gives a school.

A school without a module never meets it: its screens answer "not found" for every role,
administrators included, and nothing links to them. Only a platform administrator (a
superuser) switches a module on or off, on the platform page, and every change is logged.
"""

from functools import wraps

from django.http import Http404

MODULES = {
    "homework": {
        "field": "homework_enabled",
        "label": "Homework",
        "description": "Teachers set work, students hand it in, teachers return it; families follow along.",
    },
    "admissions": {
        "field": "admissions_enabled",
        "label": "Online admissions",
        "description": "Families apply online; the school runs the admission test, offers and enrolment.",
    },
}


def has_module(school, key):
    module = MODULES.get(key)
    return bool(school is not None and module and getattr(school, module["field"], False))


def require_module(key):
    """Guard a view of a module: "not found" wherever the school has not been given it."""

    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not has_module(getattr(request, "school", None), key):
                raise Http404
            return view(request, *args, **kwargs)

        return wrapped

    return decorate

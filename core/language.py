"""
The language of a page seen without signing in, such as the online admission form.

Signed-in people choose theirs with the header toggle. A visitor has no account to remember
it on, so the choice is kept in a cookie: English or Bangla where the school offers Bangla,
and the school's default until the visitor chooses.
"""

from django.shortcuts import redirect
from django.utils import translation

COOKIE = "public_language"


def public_language(request, school):
    """Switch this request to the visitor's language for this school, and return it."""
    language = "en"
    if school is not None and school.bangla_enabled:
        chosen = request.COOKIES.get(COOKIE, "")
        language = chosen if chosen in ("en", "bn") else (school.default_language or "en")
    translation.activate(language)
    request.LANGUAGE_CODE = language
    return language


def switch_language(request):
    """
    `?lang=bn` or `?lang=en` on a public page: remember the choice and show the page again
    without it. None when the address asks for no change.
    """
    choice = request.GET.get("lang", "")
    if choice not in ("en", "bn"):
        return None
    rest = request.GET.copy()
    rest.pop("lang", None)
    response = redirect(request.path + (f"?{rest.urlencode()}" if rest else ""))
    response.set_cookie(
        COOKIE, choice, max_age=365 * 24 * 3600, samesite="Lax", secure=request.is_secure(), httponly=True
    )
    return response

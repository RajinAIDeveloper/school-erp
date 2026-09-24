"""
What a guardian agrees to when they apply, and which wording they saw.

PDPA 2026 s.9: a child's data may be held only with a guardian's consent. The application
records the version of the wording, when it was given and from where, so the school can show
what was agreed. A change to the wording gets a new version; applications keep the old one.
"""

from django.utils.translation import gettext_lazy as _

CONSENT_VERSION = "2026-09"

CONSENT_TEXT = _(
    "I am the child's parent or guardian. I agree to the school keeping these details, and "
    "the documents I upload, to consider this application. If the child is not offered a "
    "place, the school deletes them within the period it states; if the child joins, they "
    "become part of the school record."
)

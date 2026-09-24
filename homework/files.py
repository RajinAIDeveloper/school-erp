"""
Files handed in and given out with homework.

The checks themselves are shared with every other upload (core.files): a file is judged by
what is in it, photos are redrawn without their EXIF data, and files are served only through
a checked view. What is homework's own is the size of a hand-in.
"""

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

from core.files import (  # noqa: F401 - homework's views and tests take these from here
    CONTENT_TYPES,
    EXTENSIONS,
    LONG_EDGE,
    MAX_FILE,
    MAX_PIXELS,
    prepare,
    serve,
    sniff,
    too_large,
)

MAX_FILES = 10  # pages in one hand-in
MAX_TOTAL = 25 * 1024 * 1024  # one hand-in, all pages


def prepare_all(uploads, *, already=0, already_bytes=0):
    """Check a hand-in's new files against the limits, before anything is stored."""
    uploads = [upload for upload in uploads if upload]
    if already + len(uploads) > MAX_FILES:
        raise ValidationError(gettext("A hand-in can have at most 10 pages or files."))
    prepared = [prepare(upload) for upload in uploads]
    if already_bytes + sum(content.size for content, _kind in prepared) > MAX_TOTAL:
        raise ValidationError(gettext("A hand-in can be at most 25 MB in all."))
    return prepared

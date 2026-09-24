"""
Files people upload: checked by what is in them, stored under a random name, served safely.

A file is judged by its content, never by its name: a PDF, a photo (JPEG, PNG or WebP) or,
where a screen allows it, a Word document, and nothing else. Photos are redrawn at a sensible
size as JPEG, which also drops the camera's EXIF data, the phone's location with it. Files are
served only through a checked view, never from a public address.
"""

import zipfile
from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404
from django.utils.translation import gettext

MAX_FILE = 10 * 1024 * 1024  # one file

LONG_EDGE = 2000  # a photo's longest side after redrawing, in pixels
MAX_PIXELS = 50_000_000  # anything larger is not a phone photo of a page

EXTENSIONS = {"pdf": ".pdf", "jpeg": ".jpg", "docx": ".docx"}
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "jpeg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _name(upload):
    return (getattr(upload, "name", "") or gettext("The file"))[:80]


def sniff(head):
    """What the first bytes say the file is: pdf, jpeg, png, webp, zip, or None."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    return None


def _is_word_document(upload):
    try:
        with zipfile.ZipFile(upload) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    # A document with macros is not accepted, whatever it calls itself.
    return "word/document.xml" in names and not any(name.lower().endswith("vbaproject.bin") for name in names)


def redraw(upload):
    """A photo redrawn as a JPEG no larger than LONG_EDGE, upright, without its EXIF data."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(upload) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValidationError(gettext("%(name)s is too large a picture.") % {"name": _name(upload)})
            image = ImageOps.exif_transpose(image)
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                flat = Image.new("RGB", image.size, "white")
                flat.paste(image, mask=image.getchannel("A"))
                image = flat
            else:
                image = image.convert("RGB")
            image.thumbnail((LONG_EDGE, LONG_EDGE))
            out = BytesIO()
            image.save(out, "JPEG", quality=80, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ValidationError(gettext("%(name)s could not be read as a picture.") % {"name": _name(upload)}) from exc
    return out.getvalue()


def prepare(upload, *, word=True):
    """
    Check one uploaded file and make it ready to store: (content, kind). Photos come back
    redrawn as JPEG. Anything that is not a PDF or a photo, or a Word document where `word`
    allows one, is refused.
    """
    if upload.size > MAX_FILE:
        raise ValidationError(gettext("%(name)s is larger than 10 MB.") % {"name": _name(upload)})
    upload.seek(0)
    kind = sniff(upload.read(16))
    upload.seek(0)
    if kind == "zip":
        kind = "docx" if word and _is_word_document(upload) else None
        upload.seek(0)
    if kind is None:
        if word:
            message = gettext("%(name)s is not a PDF, a photo or a Word document.")
        else:
            message = gettext("%(name)s is not a PDF or a photo.")
        raise ValidationError(message % {"name": _name(upload)})
    if kind in ("jpeg", "png", "webp"):
        return ContentFile(redraw(upload), name="page.jpg"), "jpeg"
    return ContentFile(upload.read(), name=f"file{EXTENSIONS[kind]}"), kind


def too_large(request):
    """A request over the site's upload ceiling: its files were never stored (core.uploads)."""
    from django.conf import settings

    try:
        return int(request.META.get("CONTENT_LENGTH") or 0) > settings.MAX_UPLOAD_REQUEST
    except ValueError:
        return True


def serve(field, kind, filename):
    """A checked file: photos shown in the page, documents downloaded, never run or cached."""
    try:
        handle = field.open("rb")
    except (FileNotFoundError, ValueError):
        raise Http404 from None
    response = FileResponse(
        handle, as_attachment=kind != "jpeg", filename=filename, content_type=CONTENT_TYPES.get(kind)
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    response["Cache-Control"] = "private, no-store"
    return response

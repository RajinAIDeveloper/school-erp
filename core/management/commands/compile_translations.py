"""
Compile the translation catalogues (locale/<lang>/LC_MESSAGES/django.po) into the .mo files
Django reads, without needing GNU gettext installed.

Windows machines and slim servers rarely have `msgfmt`, so the compiled files are committed
and this command rebuilds them. `--check` fails when a committed .mo no longer matches its
.po, which the test suite uses to catch a translation edited but not compiled.
"""

import ast
import struct
from array import array
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def parse_po(text):
    """{msgid: msgstr} for the translated, non-fuzzy entries of a .po file, header included."""
    entries, msgid, msgstr, section, fuzzy = {}, None, None, None, False

    def flush():
        nonlocal msgid, msgstr, section, fuzzy
        if msgid is not None and msgstr is not None and not fuzzy and (msgstr or msgid == ""):
            entries[msgid] = msgstr
        msgid, msgstr, section, fuzzy = None, None, None, False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
            continue
        if line.startswith("#"):
            continue
        if line.startswith("msgid "):
            if msgid is not None:
                flush()
            msgid, section = ast.literal_eval(line[6:]), "id"
        elif line.startswith("msgstr "):
            msgstr, section = ast.literal_eval(line[7:]), "str"
        elif line.startswith('"'):
            piece = ast.literal_eval(line)
            if section == "id":
                msgid += piece
            elif section == "str":
                msgstr += piece
    flush()
    return entries


def make_mo(entries):
    """The GNU .mo bytes for {msgid: msgstr}, in the layout Python's gettext reads."""
    keys = sorted(entries, key=lambda k: k.encode("utf-8"))
    ids, strs, offsets = b"", b"", []
    for key in keys:
        k, v = key.encode("utf-8"), entries[key].encode("utf-8")
        offsets.append((len(ids), len(k), len(strs), len(v)))
        ids += k + b"\0"
        strs += v + b"\0"
    keystart = 7 * 4 + 16 * len(keys)
    valuestart = keystart + len(ids)
    koffsets, voffsets = [], []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]
    header = struct.pack("Iiiiiii", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + len(keys) * 8, 0, 0)
    return header + array("i", koffsets).tobytes() + array("i", voffsets).tobytes() + ids + strs


def catalogues():
    for base in settings.LOCALE_PATHS:
        yield from sorted(Path(base).glob("*/LC_MESSAGES/*.po"))


class Command(BaseCommand):
    help = "Compile locale .po files to .mo without GNU gettext."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Fail if a .mo file is out of date.")

    def handle(self, *args, check=False, **options):
        stale = []
        for po in catalogues():
            compiled = make_mo(parse_po(po.read_text(encoding="utf-8")))
            mo = po.with_suffix(".mo")
            if check:
                if not mo.exists() or mo.read_bytes() != compiled:
                    stale.append(str(mo))
                continue
            mo.write_bytes(compiled)
            self.stdout.write(f"compiled {mo}")
        if stale:
            raise CommandError(f"Out of date; run compile_translations: {', '.join(stale)}")

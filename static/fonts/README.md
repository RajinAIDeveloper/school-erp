# Fonts

`NotoSansBengali-Regular.ttf` is bundled so that printed documents — report cards, admit
cards, ID cards, receipts — can render a student's Bangla name. Without it ReportLab falls
back to Helvetica, which has no Bengali glyphs, and a name silently prints as empty boxes.

Noto Sans Bengali is published by Google under the SIL Open Font License 1.1; the licence
is in `OFL.txt` and permits redistribution inside a project like this one.

`core.pdf.bangla_font()` registers whichever `NotoSansBengali*.ttf` it finds here. To swap in
a different Bengali face, drop it in with a matching name; to remove Bangla support, delete
the file and the documents fall back to Helvetica for Latin text.

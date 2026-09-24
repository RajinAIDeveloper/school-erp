"""
Charts drawn on the server as SVG.

No chart library and no script: the page arrives with its charts already drawn, so they show on
a cheap phone, print, and work with scripts off. Every chart carries a title for screen readers,
and each screen shows the same figures as a table beside it.

Percentages run 0 to 100; a missing value is simply not drawn.
"""

from django.utils.html import escape
from django.utils.safestring import mark_safe

INK = "#0f172a"
MUTED = "#64748b"
GRID = "#e2e8f0"
CHILD = "#4f46e5"  # the student
AVERAGE = "#94a3b8"  # the class average
HIGHEST = "#059669"  # the class highest
PALETTE = ["#4f46e5", "#0891b2", "#d97706", "#db2777", "#16a34a", "#7c3aed", "#dc2626", "#0d9488", "#ca8a04"]


def _f(value):
    return None if value is None else float(value)


def _short(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def bullet_chart(rows, title, labels=("Student", "Class average", "Class highest")):
    """
    One line per subject: a bar for the student's percentage, a grey tick for the class
    average and a green tick for the class highest. `rows` is [(label, value, average, highest)].
    """
    row_height, left, width = 30, 210, 420
    height = row_height * len(rows) + 46
    parts = [
        f'<svg viewBox="0 0 {left + width + 40} {height}" role="img" aria-label="{escape(title)}" '
        f'class="w-full" style="max-width:{left + width + 40}px" xmlns="http://www.w3.org/2000/svg">',
        f"<title>{escape(title)}</title>",
    ]
    for tick in (0, 25, 50, 75, 100):
        x = left + width * tick / 100
        parts.append(f'<line x1="{x:.1f}" y1="6" x2="{x:.1f}" y2="{height - 34}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{height - 22}" font-size="10" fill="{MUTED}" text-anchor="middle">{tick}</text>'
        )
    for index, (label, value, average, highest) in enumerate(rows):
        y = 8 + index * row_height
        parts.append(
            f'<text x="{left - 8}" y="{y + 15}" font-size="12" fill="{INK}" text-anchor="end">'
            f"<title>{escape(label)}</title>{escape(_short(label, 30))}</text>"
        )
        parts.append(f'<rect x="{left}" y="{y + 5}" width="{width}" height="14" rx="3" fill="#f1f5f9"/>')
        if _f(value) is not None:
            parts.append(
                f'<rect x="{left}" y="{y + 5}" width="{width * _f(value) / 100:.1f}" height="14" rx="3" fill="{CHILD}"/>'
            )
            parts.append(f'<text x="{left + width + 6}" y="{y + 16}" font-size="11" fill="{INK}">{_f(value):g}</text>')
        for mark, colour in ((average, AVERAGE), (highest, HIGHEST)):
            if _f(mark) is not None:
                x = left + width * _f(mark) / 100
                parts.append(f'<rect x="{x - 1.5:.1f}" y="{y + 1}" width="3" height="22" rx="1" fill="{colour}"/>')
    legend_y = height - 8
    for offset, (label, colour) in zip((0, 110, 240), zip(labels, (CHILD, AVERAGE, HIGHEST), strict=True), strict=True):
        parts.append(f'<rect x="{left + offset}" y="{legend_y - 9}" width="10" height="10" rx="2" fill="{colour}"/>')
        parts.append(
            f'<text x="{left + offset + 14}" y="{legend_y}" font-size="11" fill="{MUTED}">{escape(label)}</text>'
        )
    parts.append("</svg>")
    return mark_safe("".join(parts))


def line_chart(labels, series, title):
    """
    Percentages over time: `labels` are the exams in order, `series` is [(name, [value or None])]
    with the first series drawn strongest. Gaps where a value is missing are left open.
    """
    left, top, width, height = 36, 12, 560, 200
    count = max(len(labels), 1)
    step = width / max(count - 1, 1)

    def point(index, value):
        return left + (index * step if count > 1 else width / 2), top + height * (1 - _f(value) / 100)

    parts = [
        f'<svg viewBox="0 0 {left + width + 20} {top + height + 70}" role="img" aria-label="{escape(title)}" '
        f'class="w-full" style="max-width:{left + width + 20}px" xmlns="http://www.w3.org/2000/svg">',
        f"<title>{escape(title)}</title>",
    ]
    for tick in (0, 25, 50, 75, 100):
        y = top + height * (1 - tick / 100)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + width}" y2="{y:.1f}" stroke="{GRID}"/>')
        parts.append(
            f'<text x="{left - 6}" y="{y + 4:.1f}" font-size="10" fill="{MUTED}" text-anchor="end">{tick}</text>'
        )
    for index, label in enumerate(labels):
        x, _y = point(index, 0)
        # The first and last labels hang inwards, so they are never cut off at the edges.
        anchor = "middle" if 0 < index < count - 1 or count == 1 else ("start" if index == 0 else "end")
        parts.append(
            f'<text x="{x:.1f}" y="{top + height + 16}" font-size="10" fill="{MUTED}" text-anchor="{anchor}">'
            f"{escape(_short(label, 26))}</text>"
        )
    for number, (name, values) in enumerate(series):
        colour = PALETTE[number % len(PALETTE)]
        weight = 3 if number == 0 else 2
        run = []
        for index, value in enumerate(values):
            if _f(value) is None:
                if len(run) > 1:
                    parts.append(_polyline(run, colour, weight))
                run = []
                continue
            run.append(point(index, value))
        if len(run) > 1:
            parts.append(_polyline(run, colour, weight))
        for index, value in enumerate(values):
            if _f(value) is not None:
                x, y = point(index, value)
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{weight + 1}" fill="{colour}"/>')
        column, line = number % 3, number // 3
        lx, ly = left + column * 190, top + height + 36 + line * 16
        parts.append(f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" rx="2" fill="{colour}"/>')
        parts.append(f'<text x="{lx + 14}" y="{ly}" font-size="11" fill="{INK}">{escape(_short(name, 28))}</text>')
    parts.append("</svg>")
    svg = "".join(parts)
    # Make room for however many legend lines there are.
    lines = (len(series) + 2) // 3
    svg = svg.replace(
        f'viewBox="0 0 {left + width + 20} {top + height + 70}"',
        f'viewBox="0 0 {left + width + 20} {top + height + 40 + lines * 16}"',
        1,
    )
    return mark_safe(svg)


def _polyline(points, colour, weight):
    coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return (
        f'<polyline points="{coords}" fill="none" stroke="{colour}" stroke-width="{weight}" stroke-linejoin="round"/>'
    )


def shade(value):
    """A background colour for a percentage in a table cell: red when low, green when high."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "#f8fafc"  # nothing to colour: no mark, or no cell at all
    if value < 33:
        return "#fee2e2"
    if value < 50:
        return "#ffedd5"
    if value < 65:
        return "#fef9c3"
    if value < 80:
        return "#dcfce7"
    return "#bbf7d0"


def histogram(bins, title, colour=CHILD):
    """How marks spread: `bins` is [(label, count)], drawn as vertical bars with the count on top."""
    left, top, width, height = 30, 16, 560, 160
    highest = max((count for _label, count in bins), default=0) or 1
    slot = width / max(len(bins), 1)
    parts = [
        f'<svg viewBox="0 0 {left + width + 10} {top + height + 34}" role="img" aria-label="{escape(title)}" '
        f'class="w-full" style="max-width:{left + width + 10}px" xmlns="http://www.w3.org/2000/svg">',
        f"<title>{escape(title)}</title>",
        f'<line x1="{left}" y1="{top + height}" x2="{left + width}" y2="{top + height}" stroke="{GRID}"/>',
    ]
    for index, (label, count) in enumerate(bins):
        bar = height * count / highest
        x = left + index * slot + slot * 0.12
        parts.append(
            f'<rect x="{x:.1f}" y="{top + height - bar:.1f}" width="{slot * 0.76:.1f}" height="{bar:.1f}" '
            f'rx="3" fill="{colour}"/>'
        )
        if count:
            parts.append(
                f'<text x="{x + slot * 0.38:.1f}" y="{top + height - bar - 4:.1f}" font-size="11" fill="{INK}" '
                f'text-anchor="middle">{count}</text>'
            )
        parts.append(
            f'<text x="{x + slot * 0.38:.1f}" y="{top + height + 16}" font-size="10" fill="{MUTED}" '
            f'text-anchor="middle">{escape(label)}</text>'
        )
    parts.append("</svg>")
    return mark_safe("".join(parts))


def bars(rows, title):
    """Horizontal bars on a 0 to 100 scale: `rows` is [(label, value, note)], the note printed after the bar."""
    row_height, left, width = 28, 210, 380
    height = row_height * len(rows) + 26
    parts = [
        f'<svg viewBox="0 0 {left + width + 110} {height}" role="img" aria-label="{escape(title)}" '
        f'class="w-full" style="max-width:{left + width + 110}px" xmlns="http://www.w3.org/2000/svg">',
        f"<title>{escape(title)}</title>",
    ]
    for tick in (0, 50, 100):
        x = left + width * tick / 100
        parts.append(f'<line x1="{x:.1f}" y1="4" x2="{x:.1f}" y2="{height - 18}" stroke="{GRID}"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{height - 6}" font-size="10" fill="{MUTED}" text-anchor="middle">{tick}</text>'
        )
    for index, (label, value, note) in enumerate(rows):
        y = 6 + index * row_height
        parts.append(
            f'<text x="{left - 8}" y="{y + 14}" font-size="12" fill="{INK}" text-anchor="end">'
            f"<title>{escape(label)}</title>{escape(_short(label, 30))}</text>"
        )
        parts.append(f'<rect x="{left}" y="{y + 3}" width="{width}" height="14" rx="3" fill="#f1f5f9"/>')
        if _f(value) is not None:
            parts.append(
                f'<rect x="{left}" y="{y + 3}" width="{width * min(_f(value), 100) / 100:.1f}" height="14" rx="3" '
                f'fill="{CHILD}"/>'
            )
        parts.append(
            f'<text x="{left + width + 8}" y="{y + 14}" font-size="11" fill="{INK}">{escape(str(note))}</text>'
        )
    parts.append("</svg>")
    return mark_safe("".join(parts))

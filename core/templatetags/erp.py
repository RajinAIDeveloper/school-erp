from decimal import Decimal, InvalidOperation

from django import template
from django.conf import settings
from django.utils.safestring import mark_safe

from core.money import group_bd as bd_group

register = template.Library()


@register.simple_tag(takes_context=True)
def money(context, value):
    if value is None or value == "":
        return "-"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value
    symbol = context.get("CURRENCY_SYMBOL", settings.ERP_DEFAULT_CURRENCY_SYMBOL)
    return f"{symbol}{bd_group(amount)}"


@register.filter
def taka(value):
    try:
        return bd_group(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return value


@register.filter
def has_role(user, names):
    from core.roles import has_role as _has

    return _has(user, *[n.strip() for n in names.split(",")])


@register.filter
def get_item(d, key):
    if d is None:
        return None
    return d.get(key)


@register.filter
def field_type(field):
    return field.field.widget.__class__.__name__


@register.simple_tag
def active(request, *names, css="bg-slate-800 text-white"):
    """Return css if the current URL name / namespace matches any of names."""
    match = request.resolver_match
    if match and (match.url_name in names or match.app_name in names or match.namespace in names):
        return css
    return ""


@register.inclusion_tag("components/form.html")
def render_form(form):
    return {"form": form}


@register.inclusion_tag("components/field.html")
def render_field(field, label=None):
    return {"field": field, "label": label}


@register.inclusion_tag("components/pagination.html", takes_context=True)
def pagination(context, page_obj):
    return {"page_obj": page_obj, "request": context["request"]}


@register.simple_tag(takes_context=True)
def query_replace(context, **kwargs):
    q = context["request"].GET.copy()
    for k, v in kwargs.items():
        if v is None or v == "":
            q.pop(k, None)
        else:
            q[k] = v
    return q.urlencode()


BADGE_COLORS = {
    "present": "bg-emerald-100 text-emerald-800",
    "absent": "bg-red-100 text-red-800",
    "late": "bg-amber-100 text-amber-800",
    "leave": "bg-sky-100 text-sky-800",
    "half_day": "bg-amber-100 text-amber-800",
    "holiday": "bg-slate-100 text-slate-600",
    "paid": "bg-emerald-100 text-emerald-800",
    "partial": "bg-amber-100 text-amber-800",
    "unpaid": "bg-red-100 text-red-800",
    "cancelled": "bg-slate-200 text-slate-700",
    "active": "bg-emerald-100 text-emerald-800",
    "inactive": "bg-slate-200 text-slate-700",
    "pending": "bg-amber-100 text-amber-800",
    "approved": "bg-emerald-100 text-emerald-800",
    "rejected": "bg-red-100 text-red-800",
    "sent": "bg-emerald-100 text-emerald-800",
    "failed": "bg-red-100 text-red-800",
    "queued": "bg-sky-100 text-sky-800",
    "published": "bg-emerald-100 text-emerald-800",
    "draft": "bg-slate-200 text-slate-700",
    "posted": "bg-emerald-100 text-emerald-800",
    "enrolled": "bg-emerald-100 text-emerald-800",
    "promoted": "bg-sky-100 text-sky-800",
    "graduated": "bg-indigo-100 text-indigo-800",
    "transferred": "bg-amber-100 text-amber-800",
    "withdrawn": "bg-slate-200 text-slate-700",
    "resigned": "bg-slate-200 text-slate-700",
    "on_leave": "bg-sky-100 text-sky-800",
}


@register.filter
def status_badge(value):
    css = BADGE_COLORS.get(str(value).lower(), "bg-slate-100 text-slate-700")
    from django.utils.html import escape

    label = escape(str(value).replace("_", " ").title())
    return mark_safe(f'<span class="inline-flex rounded-full px-2 py-0.5 text-xs font-medium {css}">{label}</span>')


@register.filter
def lookup(obj, path):
    """Resolve dotted attribute path; calls callables; supports get_X_display."""
    cur = obj
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
            continue
        disp = getattr(cur, f"get_{part}_display", None)
        if callable(disp):
            cur = disp()
            continue
        cur = getattr(cur, part, None)
        if callable(cur):
            cur = cur()
    return cur


@register.simple_tag(takes_context=True)
def render_cell(context, obj, col):
    """col = (label, path, kind). kind in: text, money, date, datetime, badge, bool, int."""
    from django.template.defaultfilters import date as date_filter
    from django.utils.html import escape

    path, kind = col[1], (col[2] if len(col) > 2 else "text")
    value = lookup(obj, path)
    if value is None or value == "":
        return mark_safe('<span class="text-slate-400">-</span>')
    if kind == "money":
        return money(context, value)
    if kind == "date":
        return date_filter(value, "d M Y")
    if kind == "datetime":
        return date_filter(value, "d M Y, h:i A")
    if kind == "badge":
        return status_badge(value)
    if kind == "bool":
        return mark_safe("&#10004;" if value else "&#10008;")
    return escape(str(value))


@register.simple_tag
def url_for(name, obj=None):
    from django.urls import reverse

    if obj is None:
        return reverse(name)
    return reverse(name, args=[obj.pk])


@register.filter
def index(sequence, position):
    """Positional lookup for parallel lists, e.g. an attendance row against its day list."""
    try:
        return sequence[int(position)]
    except (IndexError, ValueError, TypeError, KeyError):
        return None

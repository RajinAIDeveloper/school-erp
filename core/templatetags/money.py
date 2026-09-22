from decimal import Decimal

from django import template
from django.conf import settings

register = template.Library()


def bd_group(number: Decimal) -> str:
    """Format 150000.50 -> 1,50,000.50 (Bangladeshi/Indian grouping)."""
    negative = number < 0
    number = abs(number)
    whole, _, frac = f"{number:.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    result = f"{whole}.{frac}"
    return f"-{result}" if negative else result


@register.filter
def taka(value):
    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(str(value))
    except Exception:
        return value
    return f"{settings.CURRENCY_SYMBOL}{bd_group(amount)}"

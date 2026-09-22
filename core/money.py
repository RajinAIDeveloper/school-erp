"""
Money helpers for Bangladeshi Taka.

Receipts are legal-ish documents in a school office: parents keep them, auditors read
them, and a figure written only in digits is easy to alter. So amounts are also spelled
out, using the lakh/crore grouping people here actually use.
"""

from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0.00")

ONES = [
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _under_hundred(value):
    if value < 20:
        return ONES[value]
    tens, ones = divmod(value, 10)
    return TENS[tens] + (f" {ONES[ones]}" if ones else "")


def _under_thousand(value):
    hundreds, rest = divmod(value, 100)
    words = []
    if hundreds:
        words.append(f"{ONES[hundreds]} Hundred")
    if rest:
        words.append(_under_hundred(rest))
    return " ".join(words)


def number_in_words(value):
    """Spell a whole number using crore / lakh / thousand."""
    value = int(value)
    if value == 0:
        return "Zero"
    parts = []
    for divisor, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        count, value = divmod(value, divisor)
        if count:
            # Crore can exceed 999, so it recurses through the same grouping.
            spoken = number_in_words(count) if divisor == 10_000_000 and count > 999 else _under_thousand(count)
            parts.append(f"{spoken} {label}")
    if value:
        parts.append(_under_thousand(value))
    return " ".join(parts)


def amount_in_words(amount, currency="Taka", subunit="Paisa"):
    """`Decimal('150000.50')` becomes 'One Lakh Fifty Thousand Taka and Fifty Paisa Only'."""
    amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    negative = amount < 0
    amount = abs(amount)
    whole = int(amount)
    fraction = int((amount - whole) * 100)
    words = f"{number_in_words(whole)} {currency}"
    if fraction:
        words += f" and {number_in_words(fraction)} {subunit}"
    words += " Only"
    return ("Minus " + words) if negative else words


def group_bd(number):
    """150000.50 -> 1,50,000.50 (lakh grouping)."""
    negative = number < 0
    number = abs(Decimal(str(number)))
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

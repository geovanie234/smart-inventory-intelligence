from decimal import Decimal, InvalidOperation

from django import template
from django.contrib.humanize.templatetags.humanize import intcomma

register = template.Library()


def _num(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@register.filter
def money(value, currency="RWF"):
    """RWF has no minor unit in daily use: show whole francs with thousands separators."""
    n = _num(value)
    if n is None:
        return "—"
    return f"{currency} {intcomma(int(n.quantize(Decimal('1'))))}"


@register.filter
def qty(value, places=None):
    """Drop meaningless trailing zeros: 12.000 → 12, 2.500 → 2.5."""
    n = _num(value)
    if n is None:
        return "—"
    if places is not None:
        n = n.quantize(Decimal(1).scaleb(-int(places)))
    s = format(n.normalize(), "f")
    if "." in s:
        whole, frac = s.split(".")
        return f"{intcomma(int(whole)) if whole not in ('-0',) else whole}.{frac}"
    return intcomma(int(n))


@register.filter
def pct(value, places=1):
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{int(places)}f}%"
    except (TypeError, ValueError):
        return "—"


@register.filter
def num(value, places=1):
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.{int(places)}f}"
    except (TypeError, ValueError):
        return "—"


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.simple_tag(takes_context=True)
def active(context, *prefixes):
    path = context["request"].path
    return "active" if any(path.startswith(p) for p in prefixes) else ""

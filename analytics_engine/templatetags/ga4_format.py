import math

from django import template


register = template.Library()


@register.filter
def ga4_duration(seconds):
    """
    Format seconds the way the GA4 UI does: whole seconds, truncated
    (20.74 -> "20s", 73.5 -> "1m 13s").
    """

    if seconds is None or seconds == "":
        return "—"

    total = math.floor(seconds)
    minutes, secs = divmod(total, 60)

    if minutes:
        return f"{minutes}m {secs:02d}s"

    return f"{secs}s"


@register.filter
def ga4_percent(ratio, decimals=1):
    if ratio is None or ratio == "":
        return "—"

    return f"{ratio * 100:.{int(decimals)}f}%"

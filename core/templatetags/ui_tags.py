from django import template


register = template.Library()


GROUP_EMOJI = {
    "电控": "⚡",
    "算法": "💻",
    "机械": "⚙️",
    "宣传": "🎨",
}


@register.filter
def token_unit(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return value
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


@register.filter
def get_item(mapping, key):
    if isinstance(mapping, dict):
        return mapping.get(key)
    return None


@register.filter
def group_emoji(group_name):
    """Return emoji for a lab group name."""
    return GROUP_EMOJI.get(group_name, "👤")


@register.filter
def ms_to_sec(value):
    """Convert milliseconds to seconds with appropriate precision."""
    try:
        ms = float(value or 0)
    except (TypeError, ValueError):
        return value
    sec = ms / 1000.0
    if sec >= 10:
        return f"{sec:.1f}"
    return f"{sec:.2f}"


@register.filter
def ttfb_estimate(latency_ms, completion_tokens):
    """Estimate TTFB: total_time - (output_tokens * avg_generation_rate).
    
    Assumes ~50 tokens/sec generation speed for most models.
    TTFB = latency_ms - (completion_tokens / 50 * 1000)
    """
    try:
        latency = float(latency_ms or 0)
        tokens = float(completion_tokens or 0)
    except (TypeError, ValueError):
        return "?"
    # Estimate generation time at ~50 tokens/sec
    gen_time_ms = tokens * 20  # 1000ms / 50 tokens = 20ms per token
    ttfb_ms = max(latency - gen_time_ms, 100)  # Floor at 100ms (minimum realistic TTFB)
    ttfb_sec = ttfb_ms / 1000.0
    if ttfb_sec >= 10:
        return f"{ttfb_sec:.1f}"
    return f"{ttfb_sec:.2f}"

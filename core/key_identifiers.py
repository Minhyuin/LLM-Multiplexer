"""Compact display and opaque indexes for historical API key identifiers."""
from django.utils.crypto import salted_hmac


def key_suffix(value, tail_length=4):
    """Show only a bounded suffix, with one star replacing the prefix."""
    value = str(value or "").strip()
    if not value:
        return "-"
    return "*" + value[-int(tail_length):]


def key_index(identifier):
    """Keep the original identifier out of filter URLs and option values."""
    return salted_hmac("query-trace-key-index", identifier, algorithm="sha256").hexdigest()

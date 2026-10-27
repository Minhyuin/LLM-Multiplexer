"""Public upstream interfaces only, using verified local reference files."""
from functools import lru_cache
from pathlib import Path

from django.conf import settings


def generate_challenges():
    from lmfpd import generate_challenges as generate
    return generate(3)


def parse_numbers(text):
    from lmfpd import parse_numbers as parse
    return parse(text)


def build_request(config, prompt):
    from lmfpd import build_request as build
    return build(config, prompt)


@lru_cache(maxsize=1)
def detector():
    import lmfpd
    # Loading a directory is explicitly offline in lmfpd. No default URLs or
    # automatic background data refresh are used.
    directory = Path(settings.FINGERPRINT_DATA_DIR)
    from scripts.prepare_fingerprint import verify_files
    verify_files(Path(settings.BASE_DIR))
    return lmfpd.load(str(directory))

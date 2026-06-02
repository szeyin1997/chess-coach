"""Tests for _classify_gemini_error.

Bug: a per-MINUTE rate-limit 429 was misclassified as daily-quota exhaustion,
permanently disabling the key for the process. The free-tier quota METRIC string
'generate_content_free_tier_requests' appears in BOTH per-minute and per-day
429s, so it must not drive the decision — the DIMENSION lives in the quotaId
(...PerMinutePerProject... vs ...PerDayPerProject...).
"""
from gemini_client import _classify_gemini_error

_BASE = (
    "429 RESOURCE_EXHAUSTED. {{'error': {{'code': 429, 'status': 'RESOURCE_EXHAUSTED', "
    "'details': [{{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': "
    "[{{'quotaMetric': 'generativelanguage.googleapis.com/generate_content_free_tier_requests', "
    "'quotaId': 'GenerateRequestsPer{dim}PerProjectPerModel-FreeTier'}}]}}]}}}}"
)
PER_MINUTE = _BASE.format(dim="Minute")
PER_DAY = _BASE.format(dim="Day")
TRANSIENT_503 = "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'high demand', 'status': 'UNAVAILABLE'}}"
GENERIC_429 = "429 Too Many Requests"


def test_per_minute_is_rate_limit_not_daily():
    assert _classify_gemini_error(PER_MINUTE) == "rate_limit"


def test_per_day_is_daily():
    assert _classify_gemini_error(PER_DAY) == "daily"


def test_503_is_transient():
    assert _classify_gemini_error(TRANSIENT_503) == "transient"


def test_generic_429_does_not_permanently_exhaust():
    # An ambiguous 429 with no explicit per-day dimension must NOT be treated as
    # daily exhaustion (which would disable the key for the whole process).
    assert _classify_gemini_error(GENERIC_429) == "rate_limit"

"""Retry lineage and bounded cancellation tombstone regression tests."""

import time
from unittest.mock import patch

import httpx

from claude_native_bridge.api import Owner, Owners
from claude_native_bridge.api_provider import (
    LEASES_HEADER,
    OWNER_HEADER,
    RETRY_LINEAGE_HEADER,
    make_profile,
)

TOKEN = "test-only-retry-identity-credential"
BASE_URL = "http://127.0.0.1:9/v1"


def _client(profile, headers):
    with patch("claude_native_bridge.api_service.ensure_server"):
        return profile.create_client(
            api_key=TOKEN,
            base_url=BASE_URL,
            max_retries=0,
            default_headers=headers,
            http_client=httpx.Client(trust_env=False),
        )


def test_provider_reuses_lineage_only_for_recreated_clients_of_same_parent(tmp_path):
    profile = make_profile(tmp_path)
    parent_headers = dict(profile.default_headers)
    independent_headers = dict(profile.default_headers)

    first = _client(profile, parent_headers)
    recreated = _client(profile, parent_headers)
    independent = _client(profile, independent_headers)
    try:
        first_lineage = first.default_headers[RETRY_LINEAGE_HEADER]
        assert recreated.default_headers[RETRY_LINEAGE_HEADER] == first_lineage
        assert independent.default_headers[RETRY_LINEAGE_HEADER] != first_lineage
        first_lease = first.default_headers[OWNER_HEADER]
        recreated_lease = recreated.default_headers[OWNER_HEADER]
        assert first_lease != recreated_lease
        assert set(str(first.default_headers[LEASES_HEADER]).split(",")) == {
            first_lease,
            recreated_lease,
        }
        assert independent.default_headers[OWNER_HEADER] not in str(
            first.default_headers[LEASES_HEADER]
        ).split(",")
        first.close()
        assert recreated.default_headers[LEASES_HEADER] == recreated_lease
    finally:
        first.close()
        recreated.close()
        independent.close()


def test_cancellation_tombstones_expire_and_remain_bounded(tmp_path):
    owners = Owners(lambda **kwargs: object(), tmp_path)
    old = time.monotonic() - 2
    with patch("claude_native_bridge.api.TOMBSTONE_SECONDS", 1):
        owners.tombstones[("old", "session", "request")] = old
        owners._prune_tombstones()
    assert owners.tombstones == {}

    with patch("claude_native_bridge.api.MAX_TOMBSTONES", 2):
        for index in range(3):
            owner = Owner(None, retry_key=(f"lineage-{index}", "session", "request"))
            owners._remember_failure(owner)
    assert len(owners.tombstones) == 2
    assert ("lineage-0", "session", "request") not in owners.tombstones

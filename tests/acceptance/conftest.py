"""Fixtures shared across acceptance modules (re-exported so ruff F811 stays quiet in the test files)."""

from tests.acceptance.test_bridge_acceptance import lg, relay  # noqa: F401
from tests.acceptance.test_muse_ring_acceptance import holders, world  # noqa: F401
from tests.acceptance.test_rules_common import home_fixture  # noqa: F401

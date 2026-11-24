from dataclasses import replace

import pytest
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.portfolio_lifecycle import list_activity, record_activity
from finplan_platform.handlers.api import ROUTES, route_enabled


def test_only_archive_operations_promoted_to_gamma():
    beta_only = [r for r in ROUTES if route_enabled("beta", r) and not route_enabled("prod", r)]
    promoted = {r.operation for r in beta_only if route_enabled("gamma", r)}
    assert promoted == {"record_activity_event", "list_activity_events"}


def test_archive_prod_still_refused(ctx):
    for operation, body in ((record_activity, {}), (list_activity, {})):
        with pytest.raises(PlatformError) as error:
            operation(replace(ctx, env="prod"), None, body)
        assert "beta and gamma" in str(error.value)

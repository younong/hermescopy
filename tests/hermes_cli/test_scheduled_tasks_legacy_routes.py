from starlette.routing import Match


def test_old_public_cron_management_routes_are_not_registered():
    from hermes_cli.web_server import app

    for method, path in (
        ("GET", "/api/cron/jobs"),
        ("POST", "/api/cron/jobs"),
        ("GET", "/api/cron/jobs/job-1"),
        ("PUT", "/api/cron/jobs/job-1"),
        ("POST", "/api/cron/jobs/job-1/pause"),
        ("GET", "/api/cron/delivery-targets"),
        ("GET", "/api/cron/blueprints"),
        ("POST", "/api/cron/blueprints/instantiate"),
    ):
        scope = {"type": "http", "path": path, "method": method}
        matching = [
            route
            for route in app.routes
            if route.matches(scope)[0] == Match.FULL
            and getattr(route, "path", None) != "/{full_path:path}"
        ]
        assert matching == [], (method, path, matching)

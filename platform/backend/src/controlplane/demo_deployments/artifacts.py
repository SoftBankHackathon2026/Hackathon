"""The only provider hook: a verified demo build may serve its three exact bindings.

No global image allowlist changes, copied build rows, or client-supplied grants.
An unconfigured installation has no demo journal and always returns False.
"""

from controlplane.repositories.releases import ReleaseStore


def approved_demo_image(dsn, application, environment_id, image):
    with ReleaseStore(dsn).connect() as conn:
        if not conn.execute("SELECT to_regclass('demo_deployment_runs') AS name").fetchone()[
            "name"
        ]:
            return False
        return bool(
            conn.execute(
                """SELECT 1 FROM demo_deployment_runs r
            JOIN demo_deployment_run_targets t ON t.run_id=r.id
            JOIN source_builds b ON b.id=r.build_id
            LEFT JOIN app_environments e ON e.id=t.environment_id AND e.binding IS NOT NULL
            LEFT JOIN applications a ON a.id=e.application_id
            LEFT JOIN environment_teams et ON et.environment_id=t.environment_id
            WHERE t.application=%s AND t.environment_id=%s AND r.image=%s
              AND b.image=r.image AND b.state='succeeded'
              AND b.team IS NOT DISTINCT FROM r.team
              AND (CASE WHEN e.id IS NOT NULL THEN a.team ELSE et.team END)
                  IS NOT DISTINCT FROM t.owner_team""",
                (application, environment_id, image),
            ).fetchone()
        )

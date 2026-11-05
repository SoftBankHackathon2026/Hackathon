"""The journal roots required for the next Helm deployment and its rollback."""

from controlplane.domain.models import Runtime
from controlplane.providers.onprem.helm.models import HelmPlan, PreviousHelmBinding

VERIFIED_APP = """(state='succeeded' OR (state='failed' AND error_code='release_failed_restored'))
          AND archived IS NULL"""
RETAINED_DATA = """checkpoint ? 'data_values'
          AND archived IS DISTINCT FROM 'all'"""
RETRYABLE_APP = """state='failed' AND checkpoint->>'retryable_first_deploy'='true'
          AND archived IS NULL"""


def _latest(conn, settings, condition: str):
    env = settings.environment
    return conn.execute(
        f"""SELECT * FROM releases
        WHERE application=%s AND environment_id=%s AND runtime=%s
          AND {condition}
          AND binding_fingerprint=ANY(%s)
        ORDER BY created_at DESC, id DESC LIMIT 1""",
        (env.application, env.environment_id, Runtime.ONPREM_HELM, settings.accepted_bindings()),
    ).fetchone()


def latest_verified_app(conn, settings):
    return _latest(conn, settings, VERIFIED_APP)


def latest_data(conn, settings):
    return _latest(conn, settings, RETAINED_DATA)


def latest_retryable_app(conn, settings):
    return _latest(conn, settings, RETRYABLE_APP)


def retained_bindings(conn, settings):
    """Compact under configuration_lock, using exactly the worker's journal roots.

    Keep the outgoing binding, the last verified app (possibly restored to an
    older chart), and the latest data journal, even if that deployment failed.
    This needs at most three distinct pairs, regardless of the upgrade count.
    Journal rows themselves are never deleted or rewritten.
    """
    outgoing = settings.fingerprint()
    artifacts = [settings.app_chart, *[b.app_chart for b in settings.previous_bindings]]
    approved = {
        (outgoing, chart.sha256): PreviousHelmBinding(fingerprint=outgoing, app_chart=chart)
        for chart in artifacts
    }
    approved.update({(b.fingerprint, b.app_chart.sha256): b for b in settings.previous_bindings})
    nominal = (outgoing, settings.app_chart.sha256)
    retained = {nominal: approved[nominal]}
    app = latest_verified_app(conn, settings)
    if app:
        plan = HelmPlan.model_validate(app["checkpoint"]["app_plan"])
        pair = (app["binding_fingerprint"], plan.chart_sha256)
        if pair not in approved:
            raise ValueError("Journal references an unapproved Helm binding/chart pair")
        retained[pair] = approved[pair]
    data = latest_data(conn, settings)
    if data and data["binding_fingerprint"] not in {b.fingerprint for b in retained.values()}:
        pairs = [p for p in approved if p[0] == data["binding_fingerprint"]]
        if not pairs:
            raise ValueError("Journal data references an unapproved Helm binding")
        retained[pairs[0]] = approved[pairs[0]]
    return list(retained.values())

from controlplane.domain.models import Runtime
from controlplane.execution.gcp import delete
from controlplane.execution.gcp.context import StepContext
from controlplane.execution.gcp.errors import DataProvisionError
from controlplane.execution.gcp.first_deployment import first_step
from controlplane.execution.gcp.rollout import begin_restore, begin_switch, run_rollout
from controlplane.execution.gcp.stages import provision_stages, run_migration, run_provision_stage
from controlplane.execution.gcp.states import (
    PRE_ROLLOUT_STATES,
    Action,
    ReleaseState,
    available_actions,
)
from controlplane.execution.reconciler import ReleaseReconciler
from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.gcp.models import GcpDeploymentRequest

__all__ = ["GcpWorker", "available_actions"]

DEADLINE_RESTORE_SECONDS = 300


class GcpWorker(ReleaseReconciler):
    queue_runtime = Runtime.GCP_CLOUD_RUN

    def _delete(self, deletion):
        delete.step(deletion, self.runtime, self.store)

    def _step(self, conn, row):
        rt = self.runtime
        if row["binding_fingerprint"] != rt.settings.fingerprint():
            raise ProviderConflict("Cloud Run binding changed")
        request = GcpDeploymentRequest.model_validate(row["request"])
        rt.settings.validate_request(request)
        ctx = StepContext(self, conn, row, request, dict(row["checkpoint"]))
        rt.connect()
        self._load_bindings(ctx)
        if self._handle_deadline(ctx):
            return
        if rt.settings.catalog_dynamic and self._prepare_app(ctx):
            return
        if self._handle_action(ctx):
            return
        stages = provision_stages(ctx)
        if stages:
            run_provision_stage(ctx, stages[0])
        elif run_migration(ctx):
            return
        elif ctx.initial:
            first_step(ctx)
        else:
            run_rollout(ctx)

    @staticmethod
    def _load_bindings(ctx: StepContext) -> None:
        rt, provider, cp = ctx.runtime, ctx.provider, ctx.cp
        provider.data_binding = cp.get("data_binding")
        provider.redis_binding = cp.get("redis_binding")
        provider.cache_binding = cp.get("cache_binding")
        cache_binding = provider.cache_binding
        if cache_binding and cache_binding != rt.cache.binding(cache_binding["version"]):
            raise ProviderConflict("Registered cache policy changed during deployment")

    @staticmethod
    def _handle_deadline(ctx: StepContext) -> bool:
        cp, rt = ctx.cp, ctx.runtime
        if ctx.row["expired"] and not cp.get("deadline_handled"):
            if ctx.state in PRE_ROLLOUT_STATES:
                ctx.move(
                    ReleaseState.FAILED,
                    "실행 전 배포 기한이 지났습니다.",
                    error_code="deadline_exceeded",
                )
            elif (
                Action.ROLLBACK in available_actions(ctx.row) and ctx.request.rollout.auto_rollback
            ):
                cp["deadline_handled"] = rt.now()
                begin_restore(ctx, "deadline_exceeded")
            else:
                ctx.move(
                    ReleaseState.UNCERTAIN,
                    "배포 기한 초과. 현재 트래픽을 확인하세요.",
                    error_code="deadline_exceeded",
                )
            return True
        if (
            cp.get("deadline_handled")
            and rt.now() - cp["deadline_handled"] > DEADLINE_RESTORE_SECONDS
        ):
            ctx.move(
                ReleaseState.UNCERTAIN,
                "기한 초과 후 복구를 확인하지 못했습니다.",
                error_code="restore_timeout",
            )
            return True
        return False

    @staticmethod
    def _prepare_app(ctx: StepContext) -> bool:
        app, cp = ctx.runtime.app, ctx.cp
        try:
            app.load(ctx.conn)
            ctx.initial = not app.cp.get("initialized") or cp.get("first_deployment", False)
            if ctx.initial:
                app.read(ctx.conn)
                if not app.prepare(ctx.conn):
                    ctx.poll()
                    return True
        except (DataProvisionError, ProviderConflict) as exc:
            if ctx.state not in PRE_ROLLOUT_STATES:
                raise
            ctx.move(ReleaseState.FAILED, str(exc), error_code="gcp_app_preparation_failed")
            return True
        return False

    @staticmethod
    def _handle_action(ctx: StepContext) -> bool:
        action = ctx.cp.pop("action", None)
        if action == Action.ROLLBACK:
            begin_restore(ctx, "operator_rollback")
            return True
        if action == Action.PROMOTE:
            if ctx.state != ReleaseState.AWAITING_PROMOTION:
                raise ProviderConflict("Promotion is no longer available")
            begin_switch(ctx, 100, ReleaseState.BAKING)
            return True
        return False

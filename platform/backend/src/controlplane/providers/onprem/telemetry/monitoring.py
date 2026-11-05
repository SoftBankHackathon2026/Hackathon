from controlplane.core.errors import UnsupportedOperation
from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.onprem.log_store import max_lookback
from controlplane.providers.onprem.observability import TelemetryProxy
from controlplane.providers.onprem.telemetry.connections import forget_clients, shared_clients
from controlplane.providers.onprem.telemetry.reader import NOT_CONNECTED, OnpremTelemetry


class OnpremMonitoring:
    @property
    def max_log_minutes(self):
        return max_lookback(self.settings)

    def rollout_name(self) -> str:
        raise NotImplementedError

    def _telemetry(self):
        if self.settings.observability is None:
            raise UnsupportedOperation(NOT_CONNECTED)
        name = self.rollout_name()
        clients = shared_clients(self.settings)
        proxy = TelemetryProxy(clients, self.settings.observability)
        return OnpremTelemetry(self.settings, name, clients=clients, proxy=proxy)

    def _read(self, read):
        try:
            with self._telemetry() as reader:
                return read(reader)
        except ProviderUnavailable:
            forget_clients(self.settings)
            raise

    def traffic(self, *, minutes):
        return self._read(lambda reader: reader.traffic(minutes=minutes))

    def traces(self, *, minutes, kind):
        return self._read(lambda reader: reader.traces(minutes=minutes, kind=kind))

    def trace(self, trace_id):
        return self._read(lambda reader: reader.trace(trace_id))

    def app_logs(self, *, minutes, filter_text, limit):
        return self._read(
            lambda reader: reader.app_logs(minutes=minutes, filter_text=filter_text, limit=limit)
        )

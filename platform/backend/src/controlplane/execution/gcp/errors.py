from controlplane.providers.contracts import SubmissionUncertain


class DataProvisionError(ValueError):
    """A safe, actionable message, never a cloud response body or credential."""


class MigrationError(ValueError):
    """A safe, actionable message, never a cloud response body or credential."""


class CacheSubmissionUnknown(SubmissionUncertain):
    pass

class MigrationError(Exception):
    """A fixed reason code that is safe for the platform result."""


class StorageBlocked(MigrationError):
    """Stop all writes until the destination is available again."""

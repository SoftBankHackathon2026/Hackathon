"""The API and worker use the same ADC or explicitly configured impersonation."""

import google.auth
from google.auth import impersonated_credentials


def credentials(settings):
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if settings.credentials_file:
        value, _ = google.auth.load_credentials_from_file(
            str(settings.credentials_file), scopes=scopes
        )
    else:
        value, _ = google.auth.default(scopes=scopes)
    if settings.deployer_service_account:
        value = impersonated_credentials.Credentials(
            source_credentials=value,
            target_principal=settings.deployer_service_account,
            target_scopes=scopes,
        )
    return value

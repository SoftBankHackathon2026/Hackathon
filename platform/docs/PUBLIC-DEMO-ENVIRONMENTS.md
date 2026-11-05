# Public platform demo environments

2026-10-03: the public EC2 platform receives GCP credentials and the limited on-prem
kubeconfig through GitHub Secrets → AWS Secrets Manager → root-owned server files.
`platform/deployment/provider-environments.json` contains non-secret bindings only.
CD validates these bindings before starting the provider workers.

All three providers accept the public Docker Hub digest configured in that file.
An empty ECR repository is not a prerequisite for public-image deployment. The old
all-zero ECR digest was a placeholder, not an existing deployable image.

## GCP ownership

The public platform uses the dedicated Cloud Run service `oneport-demo`, with
minimum instances 0 and maximum instances 1 (1 vCPU / 512 MiB). Its initial
bootstrap serves the same public demo image. The normal UI/Worker then creates
versioned revisions and verifies the requested release before declaring success.

The older `demo` service remains unchanged: its `blue-*` / `green-*` traffic tags
belong to the earlier bridge deployment path. The native provider deliberately
rejects such tags instead of taking over another controller's service. The failed
public-platform attempt `78fc004d045ba16d96fcc131` did not submit a Cloud Run change;
its historical `uncertain` record is retained. It is not the active environment.

A read-only snapshot conflict now fails with `cloud_run_preflight_conflict` before
any write, allowing a new request after fixing the configuration. Conflicts after
submission remain `uncertain` and continue to block blind retries.

## Verification scope

Use the public platform release history and the target URL's
`X-Launchpad-Release` header to confirm each release. Registration and running
workers alone do not prove application deployment success.

This demo has no database or Redis. It does not verify data provisioning, migration,
or the new Helm SDK's integration with the public Worker. On-prem currently uses
the native Argo provider and an internal Job to probe the preview Service.

The dedicated GCP service was bootstrapped through the Cloud Run SDK, not Terraform.
After the demo, remove only this separately identified service when it is no longer
needed; the older team's `demo` service is not a cleanup target.

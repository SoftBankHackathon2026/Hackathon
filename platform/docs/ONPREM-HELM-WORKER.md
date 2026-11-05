# On-prem Helm Worker integration

The platform now has a separate `onprem_helm` runtime. Existing `onprem_argo` applications retain their direct Argo provider and are never adopted by Helm. The registered `onprem-helm-notes` environment deploys `notes-helm-demo` at `https://notes-helm-demo.yubin.website`.

The deployment picker lists only the current on-prem environment as `notes-helm-demo · 온프렘`. The previous `onprem-demo` binding is configured with `listed: false`: its release history, worker and resources remain available, but it is omitted from the environment picker. This is a discovery setting, not an authorization boundary or resource deletion. Helm remains the implementation detail (`onprem_helm` runtime); existing app identities and data are unchanged.

## Execution

API → durable PostgreSQL release journal → Helm Worker → separate data release → authenticated Helm test → migration Job → private app release → Argo Healthy → app Helm test → public route upgrade → HTTPS 200 and exact `X-Launchpad-Release` (three consecutive samples).

The common GitHub build worker also targets this runtime: its verified public Docker Hub digest becomes the same deployment request. For the notes example select `notes-fastapi` as build context and `notes-fastapi/Dockerfile` as Dockerfile, port 8080, `/health`, PostgreSQL, and `['sh', 'scripts/start.sh', 'migrate']` as the migration argument array. The UI template already contains the deployment settings. Building an image does not create a database.

The DB address is the data release's internal PostgreSQL Service, not localhost. `DATABASE_URL` and optional `REDIS_URL` are injected through name/key/UID references to one immutable Secret. Passwords are generated once and are absent from values, release history and frontend responses. PostgreSQL is a single StatefulSet with a retained local-path PVC; Redis is a nonpersistent cache.

Each mutation follows a committed intent. The journal records chart hash, values fingerprint, Helm revision, Rollout UID, data resource UIDs and test Job identity. After a lost Helm response the worker compares the expected revision and values and does not repeat an unproven write. Missing/replaced credentials, orphan PVCs and changed data requirements stop for recovery. App failure restores a previously verified app revision; it never rolls back or deletes data.

## Artifacts and server

`platform/vendor/helm` contains the original CI `.tgz` files, `SHA256SUMS` and `SOURCE_COMMIT` from infra handoff commit `d0ec372a9a487012ca8e2dd5ff2f609896ad62ea` (app/data 0.1.1). Both hash and archive metadata are checked. The older standalone SDK can explicitly use 0.1.0; the Worker requires 0.1.1. Newer main charts do not silently replace the pinned packages.

Platform CD downloads Helm 3.22.0 for linux/amd64, checks a fixed SHA256 and packages the executable with the release. Provider activation writes `/etc/oneport/helm-runtime.json`, registers the environment, and installs `oneport-helm-worker.service`. It reuses the limited kubeconfig and platform DB secret files. No new GitHub secret or admin kubeconfig is required.

Operator command:

```sh
python -m controlplane.providers.onprem.helm.operator worker --config /etc/oneport/helm-runtime.json
```

Candidate and DB tests run as cluster Jobs, so the external Worker needs Kubernetes API and public HTTPS access, not direct `.svc.cluster.local` access. The platform journal DB remains separate from app PostgreSQL.

## Verification and limits

2026-10-03: limited-kubeconfig real-cluster Worker test on isolated app `notes-helm`:

- Initial release `b9c0e68f81907af2ee9e1295`: PostgreSQL/PVC created, authenticated DB test and migration succeeded, private candidate test passed, public `/health` returned matching release ID.
- `/ready` returned database=up; a note was written through the application API.
- Release `de361a38c71f003aec1840e9`: DB Helm revision remained 1, all saved data UIDs and credentials references remained unchanged; app revision became 3 and the saved note survived.
- Failed candidate `e3105e7311800142130e4f4a` (`APP_FORCE_UNHEALTHY=true`): Argo readiness deadline triggered recovery to the verified revision; final state `failed / release_failed_restored`, public header returned `de361a38c71f003aec1840e9`, and the saved note remained.
- Automated tests cover retained credentials/orphan resources, stale hook rejection, API intake, loss of a Helm write response, and reuse of the verified revision after rollback.

This is automatic Blue-Green integration. Manual approval, canary schedules, user-selected historical rollback and complete permanent deletion are **not wired to this Worker/UI**. Those chart capabilities must not be advertised as implemented platform operations. The public UI verification below also covers Redis. No DB HA, backups, node-failure recovery or production capacity guarantee is provided. A failed/unknown first installation is retained for recovery, rather than automatically deleting its database.

## Public server end-to-end proof (2026-10-03)

PR #58 merged as `d11b070d397507806559daa373374b9992ba0683`; Platform Deploy run `37107530115` succeeded. From the public UI, selected `onprem-helm-notes`, PostgreSQL and Redis, then submitted the public example repository (`notes-fastapi`, `notes-fastapi/Dockerfile`).

- Source build `4a23eb5a011196a7baa0085b` produced `docker.io/inhoman/oneport-release-demo@sha256:ab5a6d76168905ab42b525d74740a273c7878d57a35923e98b0833629b59e27e` from commit prefix `890872e62508`.
- Release `56c668470206f88fdcdd21d2` completed on the EC2 platform Worker without manual cluster mutation.
- Both data controllers became ready, authenticated PostgreSQL/Redis test Job completed, migration Job succeeded, and the candidate/public checks completed.
- `https://notes-helm-demo.yubin.website/health` returned HTTP 200 and the exact release header. `/ready` returned `database: up`, `cache: up`. The Notes UI stores notes in the generated PostgreSQL; its visits counter uses the provisioned Redis.

This verifies the deployed platform flow, separate from the earlier local Worker test. No new cloud database was created for this on-prem test. Test app `notes-helm` remains a separate live verification environment with its own retained PostgreSQL; it is not the public platform's registered app.

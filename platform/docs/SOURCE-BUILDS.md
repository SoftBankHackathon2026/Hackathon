# GitHub source builds

The common API accepts any public GitHub repository, ref, build context,
Dockerfile path, and an existing environment's deployment settings. A separate
worker resolves the ref to a commit, starts an isolated CodeBuild build, checks the
public Docker Hub digest and architecture, and submits an image deployment through
the existing common provider interface. Build success means the image is published;
application success is tracked by the resulting release and health checks.

## Boundaries

- API and deployment workers do not run Docker or receive Docker Hub push credentials.
- CodeBuild has one concurrent build, 15-minute build timeout, 5-minute queue timeout,
  and no app deployment, DB, or cloud-secret permissions. Its only credential is the
  build registry credential. Repositories do not need operator registration; only
  HTTPS GitHub repository URLs are accepted. Anonymous GitHub API access resolves
  the requested ref to a commit before any paid build starts. Private repositories,
  credential-bearing URLs, other hosts and redirects are rejected.
- Terraform supplies the buildspec. Repository buildspec files cannot replace it.
- Dockerfile/context must resolve inside the checkout, including symlink resolution.
- Build requests use durable IDs. StartBuild response loss retries the same idempotency
  token for at most four minutes; after that the result is uncertain and not replayed.
- Images produced by the worker are recorded for the specific app/environment; an HTTP
  caller cannot approve an arbitrary image by claiming it was built.
- A failed build does not create a release. Successful builds submit with a deterministic
  release idempotency key so a worker restart cannot submit a second deployment.
- The build result is a public image: do not bake credentials into the Dockerfile.

## Configuration and CD

Terraform: `infra/aws/platform-build.tf`. Runtime: `platform/deployment/build.json`.
GitHub Secret `ONEPORT_REGISTRY_CREDENTIALS_JSON` contains a JSON object with
`username` and `password` (a Docker Hub push token). Prefer a token restricted to
the configured output repository. Its value is never Terraform state or build arguments.
After applying infrastructure, set GitHub variable `ONEPORT_ENABLE_SOURCE_BUILDS=true`
and run Platform Deploy. CD publishes that credential to its dedicated Secrets Manager
secret and activates the fixed build-worker unit. The EC2 role can start/observe builds
and read their bounded logs but cannot read the registry credential.

`build.json` does not need a repository list. A legacy `repositories` field remains
compatible as optional UI suggestions, without restricting accepted repositories.
API and workers must run this version together to remove the old allowlist checks.
Team ownership, deployment permissions, same-environment exclusion and source/digest
verification still apply to every repository. Supported input is
`https://github.com/<owner>/<repo>` (an optional `.git` suffix or trailing slash is
normalized); GitLab, SSH and private repository authentication are not supported.

Platform daily and concurrent build caps are disabled by default (`max_daily: null`,
`max_pending: null`). Positive values can opt into an operator-defined cap. The
multi-environment run queue also defaults to no global cap. Different environments'
builds advance independently; duplicate requests and simultaneous writes to the same
environment remain blocked. The CodeBuild project has no project-specific concurrency
override; AWS account quotas still apply.

To remove an existing project's concurrency override without applying unrelated
infrastructure changes, run `AWS Infra Deploy` on `main` with operation
`remove-build-concurrency-limit`. It uses the existing infrastructure role to update
only `oneport-source-build` and verifies the resulting setting. Platform Deploy
installs both limit configurations and restarts the API and build workers.

The UI presents source fields and a Build and Deploy action only when this server-side
configuration is present. It retains the existing image-address deployment flow.

## First supported sources

Public GitHub repositories with an existing Dockerfile, linux/amd64 only. No arbitrary
build args, private submodules, SSH URLs, or Docker socket mounting are exposed.
The user made `oneport-platform-app-example` public on 2026-10-03; anonymous
GitHub API access was verified. No GitHub login token is required for this repo.
Other private repositories still require a separate read-only source integration.

Uncheck “빌드 후 바로 배포” to build and publish the image without deploying an app.
This is useful when the source app needs a database that has not been prepared yet.
A build-only result is still scoped to the selected app/environment.

For a DB-free end-to-end check, use the public `oneport-platform` repository with
context `platform/examples/release-demo` and Dockerfile
`platform/examples/release-demo/Dockerfile`. The notes examples additionally require
app DB provisioning and compatible environment/Secret bindings; building an image
does not provision its database.

Official references:
- https://docs.aws.amazon.com/codebuild/latest/userguide/build-spec-ref.html
- https://docs.aws.amazon.com/cli/latest/reference/codebuild/start-build.html

## 앱별 배포 시도 기록

앱 상세의 배포 기록은 이미지 빌드부터 배포 완료까지를 한 번의 시도로 묶습니다.
빌드에서 실패한 시도도 기록에 남으며, `release_id`로 이어진 빌드와 배포는 중복 집계하지 않습니다.
선택한 시도의 빌드·배포 단계와 로그가 오른쪽 배포 진행에 표시됩니다.
현재 서비스 중인 버전과 최근 배포 시도의 결과는 구분해서 표시합니다.

기록 목록은 한 페이지에 5개씩 표시하고 전체 개수, 현재 범위, 이전·다음 및 페이지 번호를 제공합니다.
앱별 전체 기록은 `GET /api/v1/builds`와 `GET /api/v1/releases`의 `environment_id`, `limit`, `offset`으로 조회합니다.
`limit`은 최대 100이며 기본값은 빌드 30, 배포 50입니다. 팀·환경 필터를 페이지 제한보다 먼저 적용하고 기존 소유권 검사를 유지합니다.

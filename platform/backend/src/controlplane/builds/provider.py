"""Bounded CodeBuild calls. Repository buildspecs are never used."""

import re
from urllib.parse import quote

import httpx

from controlplane.builds.models import BuildInput

COMMIT_PATTERN = re.compile("[0-9a-f]{40}")
DIGEST_PATTERN = re.compile("sha256:[0-9a-f]{64}")


class BuildRejected(ValueError):
    pass


def public_repository(repository):
    """Revalidate persisted jobs before either GitHub or CodeBuild receives a URL."""
    try:
        return BuildInput.repository_url(repository)
    except ValueError:
        raise BuildRejected("invalid_repository_url") from None


class CodeBuild:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    def resolve(self, repository, revision):
        repository = public_repository(repository)
        try:
            BuildInput.revision_name(revision)
        except ValueError:
            raise BuildRejected("invalid_source_revision") from None
        slug = repository.removeprefix("https://github.com/")
        # Anonymous API access resolves only public repositories. Redirects and
        # proxy credentials are disabled; unresolvable sources never start a build.
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as http:
            response = http.get(
                f"https://api.github.com/repos/{slug}/commits/{quote(revision, safe='')}",
                headers={"Accept": "application/vnd.github+json"},
            )
        if response.status_code != 200:
            raise BuildRejected("revision_not_found")
        commit = response.json().get("sha", "")
        if not COMMIT_PATTERN.fullmatch(commit):
            raise BuildRejected("invalid_source_commit")
        return commit

    def start(self, row):
        request, cp = row["request"], row["checkpoint"]
        repository = public_repository(request["repository"])
        if not COMMIT_PATTERN.fullmatch(cp.get("commit", "")):
            raise BuildRejected("invalid_source_commit")
        return self.client.start_build(
            projectName=self.settings.project,
            idempotencyToken=row["id"],
            sourceTypeOverride="GITHUB",
            sourceLocationOverride=repository,
            sourceVersion=cp["commit"],
            gitCloneDepthOverride=1,
            environmentVariablesOverride=[
                {"name": name, "value": value, "type": "PLAINTEXT"}
                for name, value in {
                    "ONEPORT_BUILD_ID": row["id"],
                    "ONEPORT_COMMIT": cp["commit"],
                    "ONEPORT_CONTEXT": request["context"],
                    "ONEPORT_DOCKERFILE": request["dockerfile"],
                    "ONEPORT_IMAGE": self.settings.image_repository + ":build-" + row["id"],
                }.items()
            ],
            timeoutInMinutesOverride=15,
            queuedTimeoutInMinutesOverride=5,
        )["build"]["id"]

    def observe(self, build_id):
        response = self.client.batch_get_builds(ids=[build_id])
        if len(response.get("builds", [])) != 1:
            raise BuildRejected("build_not_found")
        return response["builds"][0]

    def result_image(self, row, result):
        if result.get("resolvedSourceVersion") != row["checkpoint"]["commit"]:
            raise BuildRejected("source_commit_mismatch")
        values = {e["name"]: e["value"] for e in result.get("exportedEnvironmentVariables", [])}
        digest = values.get("ONEPORT_IMAGE_DIGEST", "")
        if not DIGEST_PATTERN.fullmatch(digest):
            raise BuildRejected("missing_image_digest")
        return self.settings.image_repository + "@" + digest

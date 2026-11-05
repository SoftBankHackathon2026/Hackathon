import httpx

from controlplane.builds.models import BuildInput
from controlplane.builds.provider import BuildRejected, CodeBuild, public_repository


class DemoSource:
    def __init__(self, settings):
        self.settings = settings

    def resolve(self, repository, revision):
        repository = public_repository(repository)
        if revision is None:
            slug = repository.removeprefix("https://github.com/")
            with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as http:
                response = http.get(
                    f"https://api.github.com/repos/{slug}",
                    headers={"Accept": "application/vnd.github+json"},
                )
            if response.status_code != 200:
                raise BuildRejected("repository_not_found")
            revision = response.json().get("default_branch")
            if not isinstance(revision, str) or not 1 <= len(revision) <= 128:
                raise BuildRejected("default_branch_not_found")
            try:
                BuildInput.revision_name(revision)
            except ValueError:
                raise BuildRejected("invalid_default_branch") from None
        return CodeBuild(self.settings, None).resolve(repository, revision)

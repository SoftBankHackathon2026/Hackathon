import re

from controlplane.execution.gcp.errors import DataProvisionError

WORKFLOW_EXECUTIONS_API = "https://workflowexecutions.googleapis.com/v1/"


def project_pattern(project_id: str) -> str:
    return rf"(?:{re.escape(project_id)}|[0-9]+)"


def execution_name(project_id: str, executions: str, name: str | None, message: str) -> str:
    suffix = re.escape(executions.split("/", 2)[2])
    match = re.fullmatch(
        rf"projects/{project_pattern(project_id)}/{suffix}/([a-zA-Z0-9-]+)", name or ""
    )
    if not match:
        raise DataProvisionError(message)
    return executions + "/" + match[1]

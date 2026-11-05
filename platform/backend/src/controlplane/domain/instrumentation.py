"""Pinned, provider-independent zero-code agent profiles.

Image config detection uses digest-verified registry metadata. Unknown/compiled
runtimes retain the OTLP SDK endpoint; no privileged eBPF injection is attempted.
"""

from functools import lru_cache

IMAGES = {
    "java": "ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-java:2.31.1@sha256:6f70427eee0ddfaaa861632eadf2d1c9ba03f4daa30e68ae352eac269e9caf7e",
    "nodejs": "ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-nodejs:0.78.0@sha256:576e2b00bdf9a6040a7e3a497dd5eea4e624f60dcfd28871d9190752a0f18955",
    "dotnet": "ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-dotnet:1.17.0@sha256:642e740b926ce7a736d674df40f7d9796451b9b018130e5dbeb2d4dd5a0f9323",
}
PATH = "/otel-auto-instrumentation"
LANGUAGES = ["auto", "python", "java", "nodejs", "dotnet", "sdk"]


def detect(config):
    env = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    command = " ".join(config.get("Entrypoint") or []) + " " + " ".join(config.get("Cmd") or [])
    if "PYTHON_VERSION" in env or any(x in command for x in ("python", "gunicorn", "uvicorn")):
        return "python"
    if "NODE_VERSION" in env or "node " in command or "npm " in command:
        return "nodejs"
    if "JAVA_HOME" in env or "java " in command:
        return "java"
    if any(k.startswith("DOTNET_") for k in env) or "dotnet " in command:
        return "dotnet"
    return "sdk"


@lru_cache(maxsize=256)
def image_language(image, architecture):
    if not image.startswith("docker.io/"):
        return "sdk"
    from controlplane.providers.public_images import verify_public_image

    return verify_public_image(image, architecture, include_runtime=True).get(
        "instrumentation", "sdk"
    )


def language(request):
    if request.tracing_enabled is False:
        return None
    if request.instrumentation != "auto":
        return request.instrumentation
    return image_language(request.source.image, request.architecture)


def agent_environment(kind, values, *, platform="glibc"):
    def append(key, value, separator=" "):
        current = values.get(key, "")
        return separator.join(filter(None, (current, value)))

    if kind == "java":
        return {
            "JAVA_TOOL_OPTIONS": append("JAVA_TOOL_OPTIONS", f"-javaagent:{PATH}/javaagent.jar")
        }
    if kind == "nodejs":
        return {
            "NODE_OPTIONS": append(
                "NODE_OPTIONS",
                f"--require={PATH}/node_modules/@opentelemetry/auto-instrumentations-node/register",
            )
        }
    if kind == "dotnet":
        if values.get("CORECLR_PROFILER") not in (None, "{918728DD-259F-4A6A-AC2B-B85E1B658318}"):
            raise ValueError("Another .NET profiler is already configured")
        native = "linux-musl" if platform == "musl" else "linux"
        return {
            "CORECLR_ENABLE_PROFILING": "1",
            "CORECLR_PROFILER": "{918728DD-259F-4A6A-AC2B-B85E1B658318}",
            "CORECLR_PROFILER_PATH": f"{PATH}/{native}/OpenTelemetry.AutoInstrumentation.Native.so",
            "DOTNET_ADDITIONAL_DEPS": append(
                "DOTNET_ADDITIONAL_DEPS", f"{PATH}/AdditionalDeps", ":"
            ),
            "DOTNET_SHARED_STORE": append("DOTNET_SHARED_STORE", f"{PATH}/store", ":"),
            "DOTNET_STARTUP_HOOKS": append(
                "DOTNET_STARTUP_HOOKS",
                f"{PATH}/net/OpenTelemetry.AutoInstrumentation.StartupHook.dll",
                ":",
            ),
            "OTEL_DOTNET_AUTO_HOME": PATH,
        }
    return {}


def copy_command(kind, target=PATH):
    source = "/javaagent.jar" if kind == "java" else "/autoinstrumentation/."
    return f"cp -r {source} {target}/"


def clean_environment(values):
    """Remove only platform-injected paths, preserving unrelated user options."""
    result = dict(values)
    for key in ("JAVA_TOOL_OPTIONS", "NODE_OPTIONS"):
        if key in result:
            # Our injected token has no whitespace. Preserve all other text/quoting.
            import re

            result[key] = re.sub(
                r"(?:^| )[^ ]*" + re.escape(PATH) + r"/[^ ]+", "", result[key]
            ).strip()
    for key in ("DOTNET_ADDITIONAL_DEPS", "DOTNET_SHARED_STORE", "DOTNET_STARTUP_HOOKS"):
        if key in result:
            result[key] = ":".join(
                v for v in result[key].split(":") if not v.startswith(PATH + "/")
            )
    if result.get("CORECLR_PROFILER_PATH", "").startswith(PATH + "/"):
        for key in (
            "CORECLR_ENABLE_PROFILING",
            "CORECLR_PROFILER",
            "CORECLR_PROFILER_PATH",
            "OTEL_DOTNET_AUTO_HOME",
        ):
            result.pop(key, None)
    return result

"""Selectable sizes shared by discovery and provider-side request validation.

Environment ceilings still apply independently. Fargate accepts discrete pairs;
Cloud Run keeps this adapter's whole-CPU/background-tracing execution policy.
"""

from typing import Literal

Cpu = Literal["0.1", "0.25", "0.5", "0.75", "1", "1.5", "2"]
MemoryMiB = Literal[
    128,
    256,
    384,
    512,
    768,
    1024,
    1280,
    1536,
    1792,
    2048,
    2304,
    2560,
    2816,
    3072,
    3328,
    3584,
    3840,
    4096,
]

# https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html
FARGATE_SIZES = {
    "0.25": [512, 1024, 2048],
    "0.5": [1024, 2048, 3072, 4096],
    "1": [2048, 3072, 4096],
    "2": [4096],
}
# Fractional CPUs require first-generation, request-based billing/concurrency=1.
# Preserve the native adapter's existing policy and its 1 GiB floor for 2 CPUs.
CLOUD_RUN_SIZES = {
    "1": list(range(512, 4097, 256)),
    "2": list(range(1024, 4097, 256)),
}
KUBERNETES_SIZES = {
    cpu: [128, 256, 384, *range(512, 4097, 256)]
    for cpu in ("0.1", "0.25", "0.5", "0.75", "1", "1.5", "2")
}


def resource_options(sizes: dict[str, list[int]]) -> dict:
    return {
        "cpu_options": list(sizes),
        "memory_options_mib": sorted({memory for values in sizes.values() for memory in values}),
        "memory_min_by_cpu": {cpu: min(values) for cpu, values in sizes.items()},
        "memory_options_by_cpu": {cpu: list(values) for cpu, values in sizes.items()},
    }


def validate_resource_pair(cpu: str, memory_mib: int, sizes: dict[str, list[int]]) -> None:
    if memory_mib not in sizes.get(cpu, []):
        raise ValueError("지원하지 않는 CPU/메모리 조합입니다.")

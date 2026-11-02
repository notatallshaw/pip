"""Measure functional-test commands after runner allocation and environment setup."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

PILOT_TESTS = (
    "tests/functional/test_install.py",
    "tests/functional/test_install_upgrade.py",
    "tests/functional/test_list.py",
)


@dataclass
class Sample:
    """One timed invocation, including its test-selection and worker settings."""

    label: str
    workers: str = "3"
    scheduler: str = "load"
    groups: int = 1
    group: int = 1


def samples(mode: str, label: str, groups: int, group: int) -> list[Sample]:
    """Use paired orders for alternatives that can share one allocated runner."""
    if mode == "workers":
        return [
            Sample("auto-first", "auto"),
            Sample("five-first", "5"),
            Sample("five-second", "5"),
            Sample("auto-second", "auto"),
        ]
    if mode == "scheduler":
        return [
            Sample("load-first"),
            Sample("worksteal-first", scheduler="worksteal"),
            Sample("worksteal-second", scheduler="worksteal"),
            Sample("load-second"),
        ]
    if mode == "os":
        return [Sample(f"{label}-first"), Sample(f"{label}-second")]
    return [Sample(label, groups=groups, group=group)]


def environment() -> dict[str, str | int | None]:
    """Record source identities, hardware and selected runner variables."""
    details: dict[str, str | int | None] = {
        "platform": platform.platform(),
        "python": sys.version,
        "cpu_count": os.cpu_count(),
    }
    for name in (
        "ImageOS",
        "ImageVersion",
        "RUNNER_ARCH",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
    ):
        details[name] = os.environ.get(name)
    for name, revision in (
        ("commit", "HEAD"),
        ("pip_tree", "HEAD:src/pip"),
        ("tests_tree", "HEAD:tests"),
    ):
        details[name] = subprocess.check_output(
            ["/usr/bin/git", "rev-parse", revision], text=True
        ).strip()
    details["hardware"] = subprocess.check_output(
        [
            "/usr/sbin/sysctl",
            "hw.ncpu",
            "hw.physicalcpu",
            "hw.logicalcpu",
            "hw.memsize",
            "machdep.cpu.brand_string",
        ],
        text=True,
    )
    return details


def command(sample: Sample, *, full_suite: bool, junit: Path) -> list[str]:
    """Run the installed test environment with the normal nox entry point."""
    return [
        sys.executable,
        "-m",
        "nox",
        "-s",
        "test-3.14",
        "--no-install",
        "--",
        *(["tests/functional"] if full_suite else PILOT_TESTS),
        "--verbose",
        "--numprocesses",
        sample.workers,
        "--dist",
        sample.scheduler,
        "--showlocals",
        "--durations=15",
        f"--junitxml={junit}",
        "--num-test-groups",
        str(sample.groups),
        "--test-group",
        str(sample.group),
    ]


def measure(arguments: list[str], log: Path) -> tuple[float, int]:
    """Time only the command, retaining its complete output and exit status."""
    with log.open("w") as stream:
        started = time.perf_counter()
        process = subprocess.run(
            arguments, stdout=stream, stderr=subprocess.STDOUT, check=False
        )
        elapsed = time.perf_counter() - started
    return elapsed, process.returncode


def write_summary(results: list[dict[str, str | int | float]]) -> None:
    """Expose execution durations separately from workflow scheduling timestamps."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary is not None:
        with Path(summary).open("a") as stream:
            stream.write(
                "| Sample | Command elapsed (seconds) | Exit status |\n"
                "| --- | ---: | ---: |\n"
            )
            stream.writelines(
                f"| {result['label']} | {result['elapsed_seconds']:.2f} "
                f"| {result['returncode']} |\n"
                for result in results
            )


def main() -> int:
    """Run the selected comparison and retain failures alongside timings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("os", "workers", "scheduler", "shards"), required=True
    )
    parser.add_argument("--label", default="comparison")
    parser.add_argument("--groups", type=int, default=1)
    parser.add_argument("--group", type=int, default=1)
    args = parser.parse_args()
    if args.groups < 1 or not 1 <= args.group <= args.groups:
        parser.error("group must be between 1 and groups")

    output = Path("macos-ci-results")
    output.mkdir(exist_ok=True)
    (output / "environment.json").write_text(json.dumps(environment(), indent=2) + "\n")
    installed = subprocess.check_output(
        [".nox/test-3-14/bin/python", "-m", "pip", "freeze", "--all"], text=True
    )
    (output / "dependencies.txt").write_text(installed)
    results: list[dict[str, str | int | float]] = []
    for sample in samples(args.mode, args.label, args.groups, args.group):
        print(f"Running {sample.label}", flush=True)
        invocation = command(
            sample,
            full_suite=args.mode == "shards",
            junit=output / f"{sample.label}.xml",
        )
        elapsed, returncode = measure(invocation, output / f"{sample.label}.log")
        result = {
            **asdict(sample),
            "elapsed_seconds": elapsed,
            "returncode": returncode,
        }
        results.append(result)
        (output / "timings.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(result), flush=True)
        if returncode:
            write_summary(results)
            return 1
    write_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

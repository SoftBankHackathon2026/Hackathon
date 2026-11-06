"""Read-only HTTP policy smoke test using synthetic fixtures, never a migration gate."""
import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request


def verify(base_url, examples, timeout=5.0):
    expected = json.loads((examples / "expected.json").read_text())
    failures = []
    for name, want in expected.items():
        policy_input = json.loads((examples / f"{name}.json").read_text())
        # Keep fixture timestamps: these exercise stale/fresh evidence deterministically.
        request = urllib.request.Request(
            base_url.rstrip("/") + "/v1/data/ghost/migration/decision",
            data=json.dumps({"input": policy_input}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                document = json.load(response)
            result = document.get("result") if isinstance(document, dict) else None
            valid = (
                isinstance(result, dict)
                and result.get("policy_version") == "migration-policy-v1"
                and result.get("outcome") == want["outcome"]
                and result.get("reason_codes") == want["reason_codes"]
                and result.get("allowed") is (want["outcome"] == "allow")
            )
            if not valid:
                failures.append(name + ": decision mismatch or policy missing")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # Do not print response bodies or connection URLs, which may contain secrets.
            failures.append(name + ": request failed or invalid JSON")
    return len(expected), failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    count, failures = verify(args.url, Path(__file__).parent / "examples", args.timeout)
    for failure in failures:
        print("FAIL " + failure)
    print(f"Synthetic policy checks: {count - len(failures)}/{count} passed")
    print("This does not verify live DB/app evidence or authorize migration.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

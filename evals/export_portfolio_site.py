"""Export the reviewed public snapshots for the static portfolio."""

import argparse
from hashlib import sha256
import json
from pathlib import Path

from evals.validate_demo_data import DEFAULT_PACKAGE, load_package, read_json


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "assets" / "data" / "portfolio.json"
PUBLIC_EVIDENCE = ROOT / "evals" / "portfolio_evidence.json"
PUBLIC_REPORT = ROOT / "evals" / "results.md"
REPOSITORY = "https://github.com/studing-endless/ai-last-mile-delivery-assistant"


def build_payload() -> dict:
    manifest = read_json(DEFAULT_PACKAGE / "manifest.json")
    cases = load_package()
    evidence = read_json(PUBLIC_EVIDENCE)
    report_text = PUBLIC_REPORT.read_text(encoding="utf-8")
    return {
        "schema_version": 1,
        "scope": manifest["scope"],
        "dataset": {key: manifest[key] for key in ("version", "license", "attribution", "source_url", "selection")},
        "links": {
            "streamlit": "https://ai-last-mile-assistant.streamlit.app",
            "source": REPOSITORY,
            "evaluation_report": f"{REPOSITORY}/blob/main/evals/results.md",
            "dataset_license": "https://creativecommons.org/licenses/by-nc/4.0/",
        },
        "delivery_cases": [{key: value for key, value in case.items() if key != "provenance"} for case in cases],
        "evaluation": {"route": evidence["route"], "ai": evidence["ai"]},
        "ai_history": evidence["cases"],
        "source_fingerprints": {
            "delivery_cases": {entry["demo_case_id"]: entry["sha256"] for entry in manifest["cases"]},
            "public_report_sha256": sha256(report_text.encode("utf-8")).hexdigest(),
            **evidence["sources"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify the existing export without changing it.")
    args = parser.parse_args()
    payload = build_payload()
    expected = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    relative_path = OUTPUT.relative_to(ROOT)
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != expected.encode("utf-8"):
            parser.exit(1, f"{relative_path} is missing or stale. Run python -m evals.export_portfolio_site.\n")
        print(f"Verified {relative_path} against the reviewed source snapshots.")
        return
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(expected, encoding="utf-8", newline="\n")
    print(f"Exported {len(payload['delivery_cases'])} delivery cases and {len(payload['ai_history'])} AI scenarios to {relative_path}.")


if __name__ == "__main__":
    main()

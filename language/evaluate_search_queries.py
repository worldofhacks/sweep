"""Replay the configured search-query cases without model or network calls."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import time
from pathlib import Path

from language.search_queries import SearchQueryFacts, resolve_search_query
from language.telemetry import NoOpTraceSink


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = Path(__file__).parent / "fixtures/search_queries.json"
    cases = json.loads(fixture.read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in cases:
        start = time.monotonic()
        facts = SearchQueryFacts(tuple(case["zones"]), ("person", "backpack", "suitcase", "bottle"))
        result = resolve_search_query(
            case["query"],
            facts,
            session_id="search-query-replay",
            correlation_id=case["id"],
            tracer=NoOpTraceSink(),
        )
        rows.append(
            {
                "case_id": case["id"],
                "source": "template",
                "model": None,
                "latency_ms": round((time.monotonic() - start) * 1000, 3),
                "cost_usd": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "passed": all(
                    getattr(result, key) == value for key, value in case["expected"].items()
                ),
                "query": case["query"],
                "expected": case["expected"],
                "actual": result.to_dict(),
            }
        )
    passed = sum(row["passed"] for row in rows)
    manifest = {
        "surface": "search-query",
        "schema_version": 1,
        "mode": "deterministic-replay",
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "cases": len(rows),
        "passed": passed,
        "cost_usd": 0,
    }
    with (args.output / "results.jsonl").open("a") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    cells = "".join(
        "<tr><td>"
        + html.escape(row["case_id"])
        + "</td><td>"
        + str(row["passed"])
        + "</td><td>"
        + str(row["latency_ms"])
        + "</td><td><details><summary>Case</summary><pre>"
        + html.escape(json.dumps(row, indent=2))
        + "</pre></details></td></tr>"
        for row in rows
    )
    (args.output / "report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Search query replay</title>'
        "<style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 16px}"
        "td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}"
        "pre{white-space:pre-wrap}</style>"
        f"<h1>Search query replay</h1><p>{passed}/{len(rows)} passed. "
        "Deterministic phrase matching; no model or network calls, $0.</p>"
        "<table><thead><tr><th>Case</th><th>Passed</th><th>Latency (ms)</th>"
        "<th>Details</th></tr></thead>"
        f"<tbody>{cells}</tbody></table>"
    )
    print(json.dumps(manifest))
    return int(passed != len(rows))


if __name__ == "__main__":
    raise SystemExit(main())

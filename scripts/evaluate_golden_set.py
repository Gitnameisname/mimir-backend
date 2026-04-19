#!/usr/bin/env python3
"""
evaluate_golden_set.py — 수동 골든셋 평가 실행 CLI

Usage:
  python scripts/evaluate_golden_set.py \
    --backend-url http://localhost:8050 \
    --golden-set-id default \
    --prompt-version v1.0 \
    --model gpt-4o \
    --output-dir results/

--prompt-version 과 --model 은 필수.
성공(overall_passed) → exit 0, 실패 → exit 1.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import requests
from requests.exceptions import RequestException

from scripts.generate_evaluation_report import (
    EvaluationReportGenerator,
    load_report_from_json,
    update_evaluation_metadata,
    _report_to_metadata,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class GoldenSetEvaluationClient:
    def __init__(
        self,
        backend_url: str,
        timeout: int = 30,
        closed_network_mode: bool = False,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.timeout = timeout
        self.closed_network_mode = closed_network_mode
        self.session = requests.Session()

    def health_check(self) -> bool:
        try:
            r = self.session.get(
                f"{self.backend_url}/api/v1/system/health", timeout=self.timeout
            )
            return r.status_code == 200
        except Exception as exc:
            logger.error("Health check failed: %s", exc)
            return False

    def start_evaluation(
        self,
        golden_set_id: str,
        batch_id: str,
        golden_items: list,
        *,
        max_concurrent: int = 5,
    ) -> str:
        payload = {
            "batch_id": batch_id,
            "golden_set_id": golden_set_id,
            "golden_items": golden_items,
            "max_concurrent": max_concurrent,
        }
        logger.info("Starting evaluation (batch_id=%s)", batch_id)
        r = self.session.post(
            f"{self.backend_url}/api/v1/evaluations/run",
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        body = r.json()
        data = body.get("data", body)
        eval_id = data.get("evaluation_id") or data.get("id")
        if not eval_id:
            raise ValueError(f"No evaluation ID in response: {body}")
        logger.info("Evaluation started: %s", eval_id)
        return eval_id

    def wait_for_completion(
        self,
        eval_id: str,
        *,
        max_wait_seconds: int = 1800,
        poll_interval: int = 10,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """평가 완료 대기. 네트워크 오류 시 max_retries 회 재시도 (VULN-P7-008)."""
        elapsed = 0
        consecutive_errors = 0

        while elapsed < max_wait_seconds:
            try:
                r = self.session.get(
                    f"{self.backend_url}/api/v1/evaluations/{eval_id}",
                    timeout=self.timeout,
                )
                r.raise_for_status()
                consecutive_errors = 0
                body = r.json()
                data = body.get("data", body)
                status = data.get("status")
                logger.info("Status: %s (%ds elapsed)", status, elapsed)
                if status == "completed":
                    return data
                if status == "failed":
                    raise ValueError(f"Evaluation {eval_id} failed")
            except ValueError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors > max_retries:
                    raise TimeoutError(
                        f"평가 상태 조회 연속 오류 {max_retries}회 초과: {exc}"
                    ) from exc
                logger.warning(
                    "평가 상태 조회 오류 (%d/%d): %s", consecutive_errors, max_retries, exc
                )

            time.sleep(poll_interval)
            elapsed += poll_interval

        raise TimeoutError(f"Evaluation did not finish within {max_wait_seconds}s")

    def get_results(self, eval_id: str) -> Dict[str, Any]:
        r = self.session.get(
            f"{self.backend_url}/api/v1/evaluations/{eval_id}",
            timeout=self.timeout,
        )
        r.raise_for_status()
        body = r.json()
        return body.get("data", body)


# ---------------------------------------------------------------------------
# Console pretty-print
# ---------------------------------------------------------------------------

def _print_summary(eval_id: str, result: Dict[str, Any]) -> None:
    width = 70
    print()
    print("=" * width)
    print("EVALUATION COMPLETE")
    print("=" * width)
    print(f"Evaluation ID : {eval_id}")
    print(f"Status        : {result.get('status', '-')}")
    print(f"Overall score : {result.get('overall_score', '-')}")

    results = result.get("results", [])
    if results:
        print()
        print(f"{'Metric':<28} {'Score':>10} {'Status':>10}")
        print("-" * width)
        # Aggregate per-item scores
        from collections import defaultdict
        totals: Dict[str, list] = defaultdict(list)
        for item in results:
            for key in ("faithfulness", "answer_relevance", "context_precision",
                        "context_recall", "citation_present", "hallucination"):
                v = item.get(key)
                if v is not None:
                    totals[key].append(float(v))
        for metric, vals in totals.items():
            avg = sum(vals) / len(vals)
            status = "✅" if avg >= 0.7 else "❌"
            print(f"{metric:<28} {avg:>10.4f} {status:>10}")
        print("-" * width)

    overall = result.get("overall_score")
    label = "✅ PASSED" if (overall if overall is not None else 0.0) >= 0.7 else "❌ CHECK RESULTS"
    print(f"\nOverall Result : {label}")
    print("=" * width)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="수동 골든셋 평가 실행")
    parser.add_argument("--backend-url", default="http://localhost:8050")
    parser.add_argument("--golden-set-id", default="default")
    parser.add_argument("--prompt-version", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--environment", default="prod", choices=["dev", "staging", "prod"])
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--json-report", help="JSON 보고서 저장 경로 (기본: output-dir/evaluation_<id>.json)")
    parser.add_argument("--markdown-report", help="마크다운 보고서 저장 경로 (기본: output-dir/AI품질평가.md)")
    parser.add_argument("--closed-network", action="store_true")
    parser.add_argument("--max-wait", type=int, default=1800)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--skip-report-generation", action="store_true")
    parser.add_argument("--dashboard-url")
    parser.add_argument("--golden-items-file", help="골든 아이템 JSON 파일 경로 (없으면 빈 배열)")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    client = GoldenSetEvaluationClient(
        backend_url=args.backend_url,
        closed_network_mode=args.closed_network,
    )

    # health check
    logger.info("Health check: %s", args.backend_url)
    if not client.health_check():
        logger.error("Backend not reachable")
        sys.exit(1)

    # load golden items
    golden_items: list = []
    if args.golden_items_file:
        with open(args.golden_items_file, encoding="utf-8") as f:
            golden_items = json.load(f)

    # start evaluation
    import uuid
    batch_id = f"{args.prompt_version}-{args.model}-{uuid.uuid4().hex[:8]}"
    try:
        eval_id = client.start_evaluation(
            golden_set_id=args.golden_set_id,
            batch_id=batch_id,
            golden_items=golden_items,
        )
    except (RequestException, ValueError) as exc:
        logger.error("Could not start evaluation: %s", exc)
        sys.exit(1)

    # wait
    try:
        result = client.wait_for_completion(
            eval_id,
            max_wait_seconds=args.max_wait,
            poll_interval=args.poll_interval,
        )
    except (TimeoutError, ValueError, RequestException) as exc:
        logger.error("Evaluation error: %s", exc)
        sys.exit(1)

    # save JSON
    json_path = Path(args.json_report or out_dir / f"evaluation_{eval_id}.json")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **result,
        "golden_set_id": args.golden_set_id,
        "prompt_version": args.prompt_version,
        "model": args.model,
        "environment": args.environment,
        "closed_network_mode": args.closed_network,
        "backend_url": args.backend_url,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info("JSON saved: %s", json_path)

    # generate markdown report
    if not args.skip_report_generation:
        try:
            report_data = load_report_from_json(str(json_path))
            gen = EvaluationReportGenerator()
            md_path = Path(args.markdown_report or out_dir / "AI품질평가.md")
            gen.generate(
                report=report_data,
                output_path=md_path,
                dashboard_url=args.dashboard_url,
                environment=args.environment,
            )
            logger.info("Markdown report: %s", md_path)
            update_evaluation_metadata(str(out_dir), _report_to_metadata(report_data, args.environment))
        except Exception as exc:
            logger.warning("Markdown report generation failed: %s", exc)

    _print_summary(eval_id, result)

    overall = result.get("overall_score", 0.0) or 0.0
    sys.exit(0 if float(overall) >= 0.7 else 1)


if __name__ == "__main__":
    main()

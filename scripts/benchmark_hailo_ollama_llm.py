#!/usr/bin/env python3
"""
Hailo-Ollama LLM benchmark.

`hailo-ollama` で実際に利用できるモデル群について、
性能と簡易品質指標を比較する。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from benchmark_utils import DataSaver, SystemMonitor
    from hailo_ollama_client import HailoOllamaClient, keyword_coverage, load_prompt_records
except ModuleNotFoundError as exc:
    if exc.name in {"benchmark_utils", "hailo_ollama_client"}:
        print(f"エラー: {exc.name}.py が見つかりません")
        print(f"期待するディレクトリ: {SCRIPT_DIR}")
        print("scripts ディレクトリ一式を同じ場所に置いたまま実行してください")
        raise SystemExit(1)
    raise


def summarize_model(rows: List[Dict[str, Any]], system_monitor: SystemMonitor) -> Dict[str, Any]:
    """モデル単位の集計"""
    successful = [row for row in rows if is_successful_row(row)]
    memory_before_values = [row["memory_before_mb"] for row in rows if row.get("memory_before_mb") is not None]
    memory_after_values = [row["memory_after_mb"] for row in successful if row.get("memory_after_mb") is not None]
    memory_delta_values = [
        row["memory_after_mb"] - row["memory_before_mb"]
        for row in successful
        if row.get("memory_after_mb") is not None and row.get("memory_before_mb") is not None
    ]
    swap_after_values = [row["swap_after_mb"] for row in successful if row.get("swap_after_mb") is not None]
    swap_delta_values = [row["swap_delta_mb"] for row in successful if row.get("swap_delta_mb") is not None]
    response_char_values = [row["response_chars"] for row in successful if row.get("response_chars") is not None]
    eval_count_values = [row["eval_count"] for row in successful if row.get("eval_count") is not None]
    keyword_coverage_values = [row["keyword_coverage"] for row in successful if row.get("keyword_coverage") is not None]
    done_reason_values = [row["done_reason"] for row in successful if row.get("done_reason")]
    error_values = [row["error"] for row in rows if row.get("error")]

    if not successful:
        return {
            "model": rows[0]["model"],
            "runs": len(rows),
            "success_rate": 0.0,
            "nonempty_response_rate": 0.0,
            "mean_ttft_ms": None,
            "p95_ttft_ms": None,
            "mean_total_ms": None,
            "mean_tokens_per_sec": None,
            "mean_keyword_coverage": None,
            "mean_response_chars": None,
            "mean_eval_count": None,
            "sample_response": "",
            "dominant_done_reason": "",
            "error_summary": error_values[0] if error_values else "",
            "mean_memory_percent": system_monitor.get_statistics().get("memory_percent", {}).get("mean"),
            "mean_memory_before_mb": round(statistics.mean(memory_before_values), 2) if memory_before_values else None,
            "mean_memory_after_mb": None,
            "mean_memory_delta_mb": None,
            "mean_swap_after_mb": None,
            "max_swap_after_mb": None,
            "mean_swap_delta_mb": None,
            "max_memory_percent": None,
        }

    ttfts = [row["ttft_ms"] for row in successful]
    totals = [row["total_ms"] for row in successful]
    tps_values = [row["tokens_per_sec"] for row in successful if row["tokens_per_sec"] > 0]
    coverage_values = keyword_coverage_values

    system_stats = system_monitor.get_statistics()

    return {
        "model": rows[0]["model"],
        "runs": len(rows),
        "success_rate": len(successful) / len(rows),
        "nonempty_response_rate": len(successful) / len(rows),
        "mean_ttft_ms": round(statistics.mean(ttfts), 2),
        "p95_ttft_ms": round(float(np.percentile(ttfts, 95)), 2),
        "mean_total_ms": round(statistics.mean(totals), 2),
        "mean_tokens_per_sec": round(statistics.mean(tps_values), 3) if tps_values else None,
        "mean_keyword_coverage": round(statistics.mean(coverage_values), 3),
        "mean_response_chars": round(statistics.mean(response_char_values), 2) if response_char_values else None,
        "mean_eval_count": round(statistics.mean(eval_count_values), 2) if eval_count_values else None,
        "sample_response": make_preview(successful[0].get("response_text", "")),
        "dominant_done_reason": most_common_value(done_reason_values),
        "error_summary": error_values[0] if error_values else "",
        "mean_memory_before_mb": round(statistics.mean(memory_before_values), 2) if memory_before_values else None,
        "mean_memory_after_mb": round(statistics.mean(memory_after_values), 2) if memory_after_values else None,
        "mean_memory_delta_mb": round(statistics.mean(memory_delta_values), 2) if memory_delta_values else None,
        "mean_memory_percent": round(system_stats.get("memory_percent", {}).get("mean", 0.0), 2),
        "max_memory_percent": round(system_stats.get("memory_percent", {}).get("max", 0.0), 2),
        "mean_swap_after_mb": round(statistics.mean(swap_after_values), 2) if swap_after_values else None,
        "max_swap_after_mb": round(max(swap_after_values), 2) if swap_after_values else None,
        "mean_swap_delta_mb": round(statistics.mean(swap_delta_values), 2) if swap_delta_values else None,
    }


def is_successful_row(row: Dict[str, Any]) -> bool:
    """実際に応答本文を返した行だけを成功とみなす"""
    if row.get("error"):
        return False
    return bool(str(row.get("response_text", "")).strip())


def make_preview(text: str, limit: int = 120) -> str:
    """CSV向けに応答プレビューを1行へ整形する"""
    normalized = " ".join(str(text).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def most_common_value(values: Sequence[str]) -> str:
    """最頻値を返す"""
    if not values:
        return ""
    return statistics.multimode(values)[0]


def build_unavailable_result(
    model_name: str,
    prompts: List[Dict[str, Any]],
    runs: int,
    reason: str,
) -> Dict[str, Any]:
    """利用可能一覧にないモデル用の結果を作る"""
    details: List[Dict[str, Any]] = []
    for record in prompts:
        for run_index in range(1, runs + 1):
            details.append(
                {
                    "model": model_name,
                    "prompt_id": record.get("id"),
                    "category": record.get("category", "general"),
                    "prompt_text": record.get("prompt", ""),
                    "expected_keywords": ",".join(record.get("expected_keywords", [])),
                    "run_index": run_index,
                    "ttft_ms": None,
                    "total_ms": None,
                    "tokens_per_sec": None,
                    "eval_count": None,
                    "prompt_eval_count": None,
                    "done_reason": "",
                    "response_chars": 0,
                    "memory_before_mb": None,
                    "memory_after_mb": None,
                    "swap_before_mb": None,
                    "swap_after_mb": None,
                    "swap_delta_mb": None,
                    "memory_percent": None,
                    "keyword_coverage": None,
                    "response_text": "",
                    "error": reason,
                }
            )
    return {"summary": summarize_model(details, SystemMonitor()), "details": details}


def benchmark_model(
    client: HailoOllamaClient,
    model_name: str,
    prompts: List[Dict[str, Any]],
    runs: int,
    max_tokens: int,
    temperature: float,
    warmup: bool,
    available_models: set[str] | None = None,
) -> Dict[str, Any]:
    """単一モデルのベンチマークを実行"""
    if available_models is not None and model_name not in available_models:
        reason = f"利用可能モデル一覧にないため未実行: {model_name}"
        print(f"  - {reason}")
        return build_unavailable_result(model_name, prompts, runs, reason)

    if warmup:
        try:
            client.chat(
                model_name=model_name,
                prompt="ウォームアップです。短く応答してください。",
                max_tokens=16,
                temperature=0.0,
            )
        except Exception as exc:  # pragma: no cover - device dependent
            print(f"  - ウォームアップ失敗: {exc}")

    details: List[Dict[str, Any]] = []
    system_monitor = SystemMonitor()

    for record in prompts:
        for run_index in range(1, runs + 1):
            before = system_monitor.measure()
            detail = {
                "model": model_name,
                "prompt_id": record.get("id"),
                "category": record.get("category", "general"),
                "prompt_text": record.get("prompt", ""),
                "expected_keywords": ",".join(record.get("expected_keywords", [])),
                "run_index": run_index,
                "memory_before_mb": round(before["memory_used_mb"], 2),
                "swap_before_mb": round(before["swap_used_mb"], 2),
                "error": "",
            }

            try:
                chat_result = client.chat(
                    model_name=model_name,
                    prompt=record["prompt"],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                after = system_monitor.measure()

                detail.update(
                    {
                        "ttft_ms": round(chat_result.ttft_ms, 2),
                        "total_ms": round(chat_result.total_ms, 2),
                        "tokens_per_sec": round(chat_result.tokens_per_sec, 3),
                        "eval_count": chat_result.eval_count,
                        "prompt_eval_count": chat_result.prompt_eval_count,
                        "done_reason": chat_result.done_reason,
                        "response_chars": len(chat_result.response_text),
                        "memory_after_mb": round(after["memory_used_mb"], 2),
                        "swap_after_mb": round(after["swap_used_mb"], 2),
                        "swap_delta_mb": round(after["swap_used_mb"] - before["swap_used_mb"], 2),
                        "memory_percent": round(after["memory_percent"], 2),
                        "keyword_coverage": round(
                            keyword_coverage(chat_result.response_text, record.get("expected_keywords", [])),
                            3,
                        ),
                        "response_text": chat_result.response_text,
                    }
                )
                if not chat_result.response_text.strip():
                    detail["error"] = "空の応答"
            except Exception as exc:  # pragma: no cover - device dependent
                after = system_monitor.measure()
                detail.update(
                    {
                        "ttft_ms": None,
                        "total_ms": None,
                        "tokens_per_sec": None,
                        "eval_count": None,
                        "prompt_eval_count": None,
                        "done_reason": "",
                        "response_chars": 0,
                        "memory_after_mb": round(after["memory_used_mb"], 2),
                        "swap_after_mb": round(after["swap_used_mb"], 2),
                        "swap_delta_mb": round(after["swap_used_mb"] - before["swap_used_mb"], 2),
                        "memory_percent": round(after["memory_percent"], 2),
                        "keyword_coverage": None,
                        "response_text": "",
                        "error": str(exc),
                    }
                )

            details.append(detail)

    summary = summarize_model(details, system_monitor)
    return {"summary": summary, "details": details}


def main() -> None:
    parser = argparse.ArgumentParser(description="Hailo-Ollama対応LLMの比較ベンチマーク")
    parser.add_argument(
        "--models",
        required=True,
        help="比較対象モデル名。カンマ区切りで指定。`all` を指定すると現在利用可能なモデルをすべて対象にする",
    )
    parser.add_argument(
        "--prompts",
        default="experiments/llm_quality_eval_prompts.jsonl",
        help="プロンプト定義ファイル（jsonl推奨）",
    )
    parser.add_argument(
        "--server",
        default="http://localhost:8000",
        help="hailo-ollamaサーバーURL",
    )
    parser.add_argument(
        "--output-prefix",
        default="results/llm/hailo_ollama_compare",
        help="出力先プレフィックス",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="各プロンプトの実行回数",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=128,
        help="最大生成トークン数",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="生成温度",
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        help="各モデルのベンチマーク前にウォームアップを行う",
    )
    parser.add_argument(
        "--check-available",
        action="store_true",
        help="ベンチマーク前に `/hailo/v1/list` を表示する",
    )
    args = parser.parse_args()

    models = [item.strip() for item in args.models.split(",") if item.strip()]
    prompts = load_prompt_records(args.prompts)
    client = HailoOllamaClient(server_url=args.server)
    available_models: list[str] | None = None

    try:
        available_models = client.list_models()
    except Exception as exc:
        if args.models.strip().lower() == "all":
            print(f"エラー: 利用可能モデル一覧を取得できませんでした: {exc}")
            raise SystemExit(1)
        print(f"注意: 利用可能モデル一覧を取得できなかったため、指定モデルをそのまま実行します: {exc}")

    if args.models.strip().lower() == "all":
        if not available_models:
            print("エラー: 利用可能なモデルが見つかりませんでした")
            raise SystemExit(1)
        models = available_models
    else:
        models = [item.strip() for item in args.models.split(",") if item.strip()]

    if args.check_available:
        print("利用可能モデル:")
        for model_name in available_models or []:
            print(f"  - {model_name}")

    all_details: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []

    for model_name in models:
        print(f"\n=== {model_name} を評価中 ===")
        result = benchmark_model(
            client=client,
            model_name=model_name,
            prompts=prompts,
            runs=args.runs,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            warmup=args.warmup,
            available_models=set(available_models) if available_models else None,
        )
        summaries.append(result["summary"])
        all_details.extend(result["details"])
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))

    ranking = sorted(
        summaries,
        key=lambda row: (
            -(row["success_rate"] or 0.0),
            -(row["mean_keyword_coverage"] or 0.0),
            row["mean_ttft_ms"] or float("inf"),
        ),
    )

    print("\n=== 推奨順（簡易） ===")
    for index, row in enumerate(ranking, start=1):
        print(
            f"{index}. {row['model']} "
            f"(品質={row['mean_keyword_coverage']}, "
            f"TTFT={row['mean_ttft_ms']}ms, "
            f"TPS={row['mean_tokens_per_sec']})"
        )

    DataSaver.save_to_csv(all_details, f"{args.output_prefix}_details.csv")
    DataSaver.save_to_csv(ranking, f"{args.output_prefix}_summary.csv")
    DataSaver.save_to_json(
        {"summaries": ranking, "details": all_details},
        f"{args.output_prefix}.json",
    )


if __name__ == "__main__":
    main()

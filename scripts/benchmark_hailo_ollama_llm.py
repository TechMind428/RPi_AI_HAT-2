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
    from benchmark_utils import (
        DataSaver,
        HailoTelemetryMonitor,
        SystemMonitor,
        flatten_hailo_telemetry,
    )
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
            "mean_cpu_percent": system_monitor.get_statistics().get("cpu_percent", {}).get("mean"),
            "max_cpu_percent": system_monitor.get_statistics().get("cpu_percent", {}).get("max"),
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
        "mean_cpu_percent": round(system_stats.get("cpu_percent", {}).get("mean", 0.0), 2),
        "max_cpu_percent": round(system_stats.get("cpu_percent", {}).get("max", 0.0), 2),
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


def markdown_escape_cell(value: Any) -> str:
    """Markdownの表セル向けに最低限エスケープする"""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def format_metric(value: Any, suffix: str = "") -> str:
    """レビューMarkdown向けにメトリクスを整形する"""
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}{suffix}"
    return f"{value}{suffix}"


def write_review_markdown(
    summaries: List[Dict[str, Any]],
    details: List[Dict[str, Any]],
    filepath: str,
) -> None:
    """目視確認用のMarkdownレビューシートを保存する"""
    output_path = Path(filepath)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    details_by_model: Dict[str, List[Dict[str, Any]]] = {}
    for row in details:
        details_by_model.setdefault(str(row.get("model", "")), []).append(row)

    lines = [
        "# LLM応答レビュー",
        "",
        "このファイルは、LLMベンチマーク結果を目視確認しやすい形に並べたものです。",
        "`summary.csv` は集計用、`details.csv` は機械処理用、このMarkdownは応答品質の確認用として使います。",
        "",
        "## サマリー",
        "",
        "| モデル | warmup_total_ms | success_rate | mean_ttft_ms | mean_total_ms | mean_tokens_per_sec | mean_keyword_coverage | mean_cpu_percent | mean_swap_after_mb | hailo_temp_c_mean | hailo_ram_used_mb_mean | hailo_nnc_utilization_mean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for row in summaries:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_escape_cell(row.get("model")),
                    format_metric(row.get("warmup_total_ms")),
                    format_metric(row.get("success_rate")),
                    format_metric(row.get("mean_ttft_ms")),
                    format_metric(row.get("mean_total_ms")),
                    format_metric(row.get("mean_tokens_per_sec")),
                    format_metric(row.get("mean_keyword_coverage")),
                    format_metric(row.get("mean_cpu_percent")),
                    format_metric(row.get("mean_swap_after_mb")),
                    format_metric(row.get("hailo_temp_c_mean")),
                    format_metric(row.get("hailo_ram_used_mb_mean")),
                    format_metric(row.get("hailo_nnc_utilization_mean")),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 目視確認の観点",
            "",
            "- 日本語の説明が大きく破綻していないか",
            "- プロンプトにない推測を足していないか",
            "- 指定された形式、文字数、文数におおむね従っているか",
            "- 期待キーワードを含んでいるか",
            "- 応答が短すぎたり、途中で切れていたりしないか",
            "",
        ]
    )

    for summary in summaries:
        model_name = str(summary.get("model", ""))
        lines.extend([f"## {model_name}", ""])
        model_details = details_by_model.get(model_name, [])

        for row in model_details:
            prompt_id = row.get("prompt_id", "")
            run_index = row.get("run_index", "")
            category = row.get("category", "")
            expected_keywords = row.get("expected_keywords", "")
            response_text = row.get("response_text", "")
            error = row.get("error", "")

            lines.extend(
                [
                    f"### {prompt_id} / run {run_index}",
                    "",
                    f"- category: `{category}`",
                    f"- expected_keywords: `{expected_keywords}`",
                    f"- keyword_coverage: `{format_metric(row.get('keyword_coverage'))}`",
                    f"- ttft_ms: `{format_metric(row.get('ttft_ms'))}`",
                    f"- total_ms: `{format_metric(row.get('total_ms'))}`",
                    f"- tokens_per_sec: `{format_metric(row.get('tokens_per_sec'))}`",
                    f"- done_reason: `{markdown_escape_cell(row.get('done_reason'))}`",
                    "",
                    "**Prompt**",
                    "",
                    "```text",
                    str(row.get("prompt_text", "")),
                    "```",
                    "",
                    "**Response**",
                    "",
                    "```text",
                    str(response_text),
                    "```",
                    "",
                ]
            )

            if error:
                lines.extend(["**Error**", "", "```text", str(error), "```", ""])

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"レビュー用Markdownを保存しました: {output_path}")


def most_common_value(values: Sequence[str]) -> str:
    """最頻値を返す"""
    if not values:
        return ""
    return statistics.multimode(values)[0]


def add_latest_hailo_telemetry(detail: Dict[str, Any], latest: Dict[str, Any] | None) -> None:
    """詳細ログへ直近のHAT側テレメトリを付ける"""
    if not latest:
        return

    for key, value in latest.items():
        if key in {"timestamp", "datetime"}:
            detail[f"hailo_{key}"] = value
        elif key.startswith("hailo_"):
            detail[key] = value


def empty_warmup_summary(enabled: bool = False, error: str = "") -> Dict[str, Any]:
    """ウォームアップ未実行または失敗時の集計列を返す"""
    return {
        "warmup_enabled": enabled,
        "warmup_success": False,
        "warmup_ttft_ms": None,
        "warmup_total_ms": None,
        "warmup_tokens_per_sec": None,
        "warmup_eval_count": None,
        "warmup_response_chars": None,
        "warmup_done_reason": "",
        "warmup_error": error,
    }


def run_warmup(
    client: HailoOllamaClient,
    model_name: str,
) -> Dict[str, Any]:
    """本測定前に短い推論を1回流し、初回応答時間を記録する"""
    try:
        result = client.chat(
            model_name=model_name,
            prompt="ウォームアップです。短く応答してください。",
            max_tokens=16,
            temperature=0.0,
        )
    except Exception as exc:  # pragma: no cover - device dependent
        error = str(exc)
        print(f"  - ウォームアップ失敗: {error}")
        return empty_warmup_summary(enabled=True, error=error)

    warmup_summary = {
        "warmup_enabled": True,
        "warmup_success": bool(result.response_text.strip()),
        "warmup_ttft_ms": round(result.ttft_ms, 2),
        "warmup_total_ms": round(result.total_ms, 2),
        "warmup_tokens_per_sec": round(result.tokens_per_sec, 3),
        "warmup_eval_count": result.eval_count,
        "warmup_response_chars": len(result.response_text),
        "warmup_done_reason": result.done_reason,
        "warmup_error": "" if result.response_text.strip() else "空の応答",
    }
    print(
        "  - ウォームアップ: "
        f"total={warmup_summary['warmup_total_ms']}ms, "
        f"TTFT={warmup_summary['warmup_ttft_ms']}ms, "
        f"TPS={warmup_summary['warmup_tokens_per_sec']}"
    )
    return warmup_summary


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
                    "cpu_before_percent": None,
                    "memory_after_mb": None,
                    "cpu_after_percent": None,
                    "swap_before_mb": None,
                    "swap_after_mb": None,
                    "swap_delta_mb": None,
                    "memory_percent": None,
                    "keyword_coverage": None,
                    "response_text": "",
                    "error": reason,
                }
            )
    summary = summarize_model(details, SystemMonitor())
    summary.update(empty_warmup_summary(enabled=False, error=reason))
    return {"summary": summary, "details": details}


def attach_warmup_summary(summary: Dict[str, Any], warmup_summary: Dict[str, Any]) -> Dict[str, Any]:
    """モデル単位の集計へウォームアップ結果を付ける"""
    summary.update(warmup_summary)
    return summary


def benchmark_model(
    client: HailoOllamaClient,
    model_name: str,
    prompts: List[Dict[str, Any]],
    runs: int,
    max_tokens: int,
    temperature: float,
    warmup: bool,
    available_models: set[str] | None = None,
    hat_telemetry: bool = False,
    hat_telemetry_sampling_ms: int = 100,
    hat_telemetry_poll_interval: float = 1.0,
) -> Dict[str, Any]:
    """単一モデルのベンチマークを実行"""
    if available_models is not None and model_name not in available_models:
        reason = f"利用可能モデル一覧にないため未実行: {model_name}"
        print(f"  - {reason}")
        return build_unavailable_result(model_name, prompts, runs, reason)

    warmup_summary = run_warmup(client, model_name) if warmup else empty_warmup_summary(enabled=False)

    details: List[Dict[str, Any]] = []
    system_monitor = SystemMonitor()
    hailo_monitor = HailoTelemetryMonitor(
        enabled=hat_telemetry,
        sampling_period_ms=hat_telemetry_sampling_ms,
        poll_interval_sec=hat_telemetry_poll_interval,
    )

    try:
        hailo_monitor.start()

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
                    "cpu_before_percent": round(before["cpu_percent"], 2),
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
                            "cpu_after_percent": round(after["cpu_percent"], 2),
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
                            "cpu_after_percent": round(after["cpu_percent"], 2),
                            "memory_after_mb": round(after["memory_used_mb"], 2),
                            "swap_after_mb": round(after["swap_used_mb"], 2),
                            "swap_delta_mb": round(after["swap_used_mb"] - before["swap_used_mb"], 2),
                            "memory_percent": round(after["memory_percent"], 2),
                            "keyword_coverage": None,
                            "response_text": "",
                            "error": str(exc),
                        }
                    )

                add_latest_hailo_telemetry(detail, hailo_monitor.latest())
                details.append(detail)
    finally:
        hailo_monitor.stop()

    hailo_telemetry_stats = hailo_monitor.get_statistics()
    hailo_telemetry_measurements = hailo_monitor.snapshot()
    hailo_monitor.close()

    summary = attach_warmup_summary(summarize_model(details, system_monitor), warmup_summary)
    summary.update(flatten_hailo_telemetry(hailo_telemetry_stats))
    return {
        "summary": summary,
        "details": details,
        "hailo_telemetry": hailo_telemetry_stats,
        "hailo_telemetry_measurements": hailo_telemetry_measurements,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Hailo-Ollama対応LLMの比較ベンチマーク")
    parser.add_argument(
        "--models",
        required=True,
        help="比較対象モデル名。カンマ区切りで指定。`all` を指定すると現在利用可能なモデルをすべて対象にする",
    )
    parser.add_argument(
        "--prompts",
        default="scripts/llm_quality_eval_prompts.jsonl",
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
    parser.add_argument(
        "--hat-telemetry",
        action="store_true",
        help="HAT側の温度、RAM、NNC使用率も測定する",
    )
    parser.add_argument(
        "--hat-telemetry-sampling-ms",
        type=int,
        default=100,
        help="HAT側テレメトリのサンプリング時間（ミリ秒）",
    )
    parser.add_argument(
        "--hat-telemetry-poll-interval",
        type=float,
        default=1.0,
        help="HAT側テレメトリを読む間隔（秒）",
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
    telemetry_by_model: Dict[str, Any] = {}

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
            hat_telemetry=args.hat_telemetry,
            hat_telemetry_sampling_ms=args.hat_telemetry_sampling_ms,
            hat_telemetry_poll_interval=args.hat_telemetry_poll_interval,
        )
        summaries.append(result["summary"])
        all_details.extend(result["details"])
        telemetry_by_model[model_name] = {
            "summary": result.get("hailo_telemetry", {}),
            "measurements": result.get("hailo_telemetry_measurements", []),
        }
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
    write_review_markdown(ranking, all_details, f"{args.output_prefix}_review.md")
    DataSaver.save_to_json(
        {
            "summaries": ranking,
            "details": all_details,
            "hailo_telemetry_by_model": telemetry_by_model,
        },
        f"{args.output_prefix}.json",
    )


if __name__ == "__main__":
    main()

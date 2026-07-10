#!/usr/bin/env python3
"""
COCO80ラベルをLLMに渡し、危険物として判定するかを調べる。

YOLOの検出性能ではなく、LLMがラベル名だけから何を危険とみなすかを切り分ける。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from hailo_ollama_client import HailoOllamaClient
from multimodal_voice_assistant import COCO80_JA_NAMES, COCO80_NAMES


DEFAULT_PROMPT_TEMPLATE = (
    "検出結果: {label_ja}。"
    "これは机の上にあると危険ですか。"
    "はい/いいえで始めて、理由を1文で答えてください。"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="COCO80ラベルの危険物判定をLLMで評価する")
    parser.add_argument("--server", default="http://localhost:8000", help="hailo-ollama のURL")
    parser.add_argument("--model", default="qwen3:1.7b", help="評価するLLMモデル")
    parser.add_argument("--runs", type=int, default=3, help="各ラベルの評価回数")
    parser.add_argument("--max-tokens", type=int, default=128, help="LLMの最大出力トークン数")
    parser.add_argument("--temperature", type=float, default=0.0, help="LLMのtemperature")
    parser.add_argument("--output", default="~/results/chapter6/coco80_risk_llm.csv", help="CSV保存先")
    parser.add_argument("--markdown", default=None, help="レビュー用Markdown保存先。未指定ならCSVと同じ名前の.md")
    parser.add_argument("--labels", default=None, help="評価対象の英語ラベルをカンマ区切りで限定する")
    parser.add_argument("--sleep", type=float, default=0.0, help="各リクエスト後の待ち時間秒")
    parser.add_argument("--retries", type=int, default=2, help="HTTPエラー時の再試行回数")
    parser.add_argument("--retry-wait", type=float, default=2.0, help="HTTPエラー時の再試行待ち秒")
    parser.add_argument("--show-prompt", action="store_true", help="各ラベルでLLMへ渡すプロンプトを表示する")
    return parser.parse_args()


def selected_labels(label_filter: str | None) -> List[str]:
    if not label_filter:
        return list(COCO80_NAMES)

    requested = [label.strip() for label in label_filter.split(",") if label.strip()]
    unknown = [label for label in requested if label not in COCO80_NAMES]
    if unknown:
        raise SystemExit(f"未知のCOCO80ラベルです: {', '.join(unknown)}")
    return requested


def build_prompt(label_en: str) -> str:
    label_ja = COCO80_JA_NAMES.get(label_en, label_en)
    return re.sub(r"\s+", " ", DEFAULT_PROMPT_TEMPLATE.format(label_en=label_en, label_ja=label_ja)).strip()


def parse_judgement(response_text: str) -> tuple[str, str]:
    judgement = "unknown"
    reason = response_text.strip()
    answer_text = extract_answer_text(response_text)

    j_matches = list(re.finditer(r"\bJ\s*[:：]\s*(yes|no)\b", answer_text, flags=re.IGNORECASE))
    if j_matches:
        judgement = j_matches[-1].group(1).lower()
    else:
        judgement_matches = list(
            re.finditer(r"判定\s*[:：]\s*(yes|no)\b", answer_text, flags=re.IGNORECASE)
        )
        if judgement_matches:
            judgement = judgement_matches[-1].group(1).lower()
        else:
            first_line = answer_text.strip().splitlines()[0] if answer_text.strip() else ""
            if re.fullmatch(r"\s*yes\b.*", first_line, flags=re.IGNORECASE):
                judgement = "yes"
            elif re.fullmatch(r"\s*no\b.*", first_line, flags=re.IGNORECASE):
                judgement = "no"
            elif re.match(r"\s*はい\b|^\s*はい[、。]", first_line):
                judgement = "yes"
            elif re.match(r"\s*いいえ\b|^\s*いいえ[、。]", first_line):
                judgement = "no"
            else:
                judgement = infer_judgement_from_japanese(answer_text)

    r_matches = list(re.finditer(r"\bR\s*[:：]\s*(.+)", answer_text, flags=re.IGNORECASE | re.DOTALL))
    if r_matches:
        reason = r_matches[-1].group(1).strip()
    else:
        reason_matches = list(re.finditer(r"理由\s*[:：]\s*(.+)", answer_text, flags=re.DOTALL))
        if reason_matches:
            reason = reason_matches[-1].group(1).strip()
        else:
            reason = answer_text

    reason = re.sub(r"\s+", " ", reason)
    return judgement, reason


def extract_answer_text(response_text: str) -> str:
    """プロンプトを復唱した場合に備え、回答部分だけをできる範囲で取り出す。"""
    text = response_text.strip()
    marker = "見えている物だけを根拠に、日本語で答えてください。"
    if marker in text:
        text = text.rsplit(marker, 1)[-1].strip()
    return text or response_text.strip()


def infer_judgement_from_japanese(text: str) -> str:
    """自然文回答から危険判定を推定する。推定できない場合は unknown。"""
    normalized = normalize_for_judgement(text)
    no_regexes = [
        r"危険なものは[^。]*ありません",
        r"危険な物は[^。]*ありません",
        r"危険なものは[^。]*ない",
        r"危険な物は[^。]*ない",
        r"危険なものは[^。]*ではありません",
        r"危険な物は[^。]*ではありません",
        r"危険なものは[^。]*ではない",
        r"危険な物は[^。]*ではない",
        r"危険ではありません",
        r"危険ではない",
    ]
    no_patterns = [
        "危険なものはありません",
        "危険な物はありません",
        "危険なものはない",
        "危険な物はない",
        "危険なものではない",
        "危険な物ではない",
        "危険ではありません",
        "危険ではない",
        "危険なものではありません",
        "危険な物ではありません",
        "危険性は低い",
        "安全な物",
        "安全なもの",
        "危険な物とは考えにくい",
        "危険なものとは考えにくい",
    ]
    yes_patterns = [
        "はい",
        "危険なものは",
        "危険な物は",
        "危険です",
        "危険なものです",
        "危険な物です",
        "危険があります",
        "注意が必要",
        "刃",
        "けが",
        "怪我",
        "燃え",
        "火傷",
    ]

    if any(re.search(pattern, normalized) for pattern in no_regexes):
        return "no"
    if any(pattern in normalized for pattern in no_patterns):
        return "no"
    if normalized.startswith("いいえ"):
        return "no"
    if normalized.startswith("はい"):
        return "yes"
    if any(pattern in normalized for pattern in yes_patterns):
        return "yes"
    return "unknown"


def normalize_for_judgement(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[「」『』（）()【】\[\] 　\t\r\n]", "", text)
    return text


def one_line(value: object) -> str:
    """CSVを表計算ソフトで扱いやすいよう、セル内改行を取り除く。"""
    return re.sub(r"\s+", " ", str(value)).strip()


def generate_with_retry(
    client: HailoOllamaClient,
    *,
    model_name: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    retries: int,
    retry_wait: float,
):
    last_error: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            return client.generate(
                model_name=model_name,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            last_error = exc
            if attempt > retries:
                break
            print(f"    retry {attempt}/{retries}: {exc}")
            time.sleep(retry_wait)
    raise last_error if last_error is not None else RuntimeError("LLM呼び出しに失敗しました")


def write_csv(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "model",
        "label_en",
        "label_ja",
        "run",
        "judgement",
        "reason",
        "ttft_ms",
        "total_ms",
        "tokens_per_sec",
        "eval_count",
        "prompt",
        "raw_response",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    by_label: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_label[str(row["label_en"])].append(row)

    summary_rows: List[Dict[str, object]] = []
    for label_en in COCO80_NAMES:
        label_rows = by_label.get(label_en, [])
        if not label_rows:
            continue
        counts = Counter(str(row["judgement"]) for row in label_rows)
        majority = counts.most_common(1)[0][0]
        yes_count = counts.get("yes", 0)
        no_count = counts.get("no", 0)
        unknown_count = counts.get("unknown", 0)
        error_count = counts.get("error", 0)
        summary_rows.append(
            {
                "label_en": label_en,
                "label_ja": COCO80_JA_NAMES.get(label_en, label_en),
                "majority": majority,
                "yes_count": yes_count,
                "no_count": no_count,
                "unknown_count": unknown_count,
                "error_count": error_count,
                "runs": len(label_rows),
                "sample_reason": str(label_rows[-1]["reason"]),
            }
        )
    return summary_rows


def write_markdown(path: Path, model: str, rows: List[Dict[str, object]]) -> None:
    summary_rows = summarize(rows)
    risky = [row for row in summary_rows if row["majority"] == "yes"]
    unstable = [
        row for row in summary_rows
        if row["yes_count"] > 0 and row["no_count"] > 0
    ]

    lines = [
        "# COCO80 危険物判定LLM評価",
        "",
        f"- モデル: `{model}`",
        f"- 評価ラベル数: {len(summary_rows)}",
        f"- 危険判定多数: {len(risky)}",
        f"- 判定揺れあり: {len(unstable)}",
        "",
        "## 危険判定が多数だったラベル",
        "",
        "| label_en | label_ja | yes | no | unknown | error | 理由例 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]

    for row in risky:
        lines.append(
            f"| {row['label_en']} | {row['label_ja']} | {row['yes_count']} | "
            f"{row['no_count']} | {row['unknown_count']} | {row['error_count']} | {row['sample_reason']} |"
        )

    if unstable:
        lines.extend([
            "",
            "## 判定が揺れたラベル",
            "",
            "| label_en | label_ja | yes | no | unknown | error | 理由例 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ])
        for row in unstable:
            lines.append(
                f"| {row['label_en']} | {row['label_ja']} | {row['yes_count']} | "
                f"{row['no_count']} | {row['unknown_count']} | {row['error_count']} | {row['sample_reason']} |"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    output_path = Path(args.output).expanduser()
    markdown_path = Path(args.markdown).expanduser() if args.markdown else output_path.with_suffix(".md")
    labels = selected_labels(args.labels)
    client = HailoOllamaClient(server_url=args.server, timeout=240)
    rows: List[Dict[str, object]] = []

    for label_index, label_en in enumerate(labels, start=1):
        label_ja = COCO80_JA_NAMES.get(label_en, label_en)
        prompt = build_prompt(label_en)
        print(f"[{label_index}/{len(labels)}] {label_en} / {label_ja}")
        if args.show_prompt:
            print("  === prompt ===")
            print(f"  {prompt}")
            print("  === prompt end ===")

        for run in range(1, args.runs + 1):
            try:
                result = generate_with_retry(
                    client,
                    model_name=args.model,
                    prompt=prompt,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    retries=args.retries,
                    retry_wait=args.retry_wait,
                )
                raw_response = one_line(result.response_text)
                judgement, reason = parse_judgement(raw_response)
                ttft_ms = f"{result.ttft_ms:.1f}"
                total_ms = f"{result.total_ms:.1f}"
                tokens_per_sec = f"{result.tokens_per_sec:.2f}"
                eval_count = result.eval_count
            except Exception as exc:
                raw_response = one_line(exc)
                judgement = "error"
                reason = one_line(str(exc).splitlines()[0])
                ttft_ms = ""
                total_ms = ""
                tokens_per_sec = ""
                eval_count = ""
            print(f"  run {run}: {judgement} - {reason}")
            rows.append(
                {
                    "model": args.model,
                    "label_en": label_en,
                    "label_ja": label_ja,
                    "run": run,
                    "judgement": judgement,
                    "reason": one_line(reason),
                    "ttft_ms": ttft_ms,
                    "total_ms": total_ms,
                    "tokens_per_sec": tokens_per_sec,
                    "eval_count": eval_count,
                    "prompt": one_line(prompt),
                    "raw_response": raw_response,
                }
            )
            if args.sleep > 0:
                time.sleep(args.sleep)

        write_csv(output_path, rows)
        write_markdown(markdown_path, args.model, rows)

    print(f"CSVを保存しました: {output_path}")
    print(f"Markdownを保存しました: {markdown_path}")


if __name__ == "__main__":
    main()

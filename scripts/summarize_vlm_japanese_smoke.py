#!/usr/bin/env python3
"""Summarize VLM Japanese smoke-test CSV for the chapter 6 column."""

import argparse
import csv
import re
from pathlib import Path


JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
MOJIBAKE_RE = re.compile(r"[�㌢]")
SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]+\|>")


def clean_response(text: str) -> str:
    return SPECIAL_TOKEN_RE.sub("", text).strip()


def jp_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(JP_RE.findall(text)) / len(text)


def compact(text: str, limit: int) -> str:
    one_line = " ".join(clean_response(str(text)).split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


def observation(response: str) -> str:
    notes = []
    if SPECIAL_TOKEN_RE.search(response):
        notes.append("終端トークン残り")
    tokens = re.findall(r"[\u3040-\u30ff\u3400-\u9fffA-Za-z0-9]+", response)
    for token in set(tokens):
        if len(token) >= 2 and tokens.count(token) >= 4:
            notes.append("反復が目立つ")
            break
    if "時計の上に" in response and response.count("時計の上に") >= 2:
        notes.append("反復が目立つ")
    if "壁に" in response and response.count("壁に") >= 2:
        notes.append("反復が目立つ")
    ratio = jp_ratio(response)
    if MOJIBAKE_RE.search(response):
        notes.append("文字化け/不自然語の疑い")
    if ratio < 0.25:
        notes.append("日本語応答として弱い")
    if len(response.strip()) < 20:
        notes.append("説明量が少ない")
    return " / ".join(dict.fromkeys(notes)) if notes else "要目視確認"


def main():
    parser = argparse.ArgumentParser(description="VLM日本語スモークテスト結果をMarkdown表にする")
    parser.add_argument("--input", required=True, help="vlm_japanese_smoke.csv")
    parser.add_argument("--output", required=True, help="出力Markdown")
    parser.add_argument("--excerpt-chars", type=int, default=120)
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()

    rows = list(csv.DictReader(input_path.open(encoding="utf-8-sig")))
    if not rows:
        raise RuntimeError(f"行がありません: {input_path}")

    has_run_id = any(row.get("run_id") for row in rows)

    lines = [
        "# VLM日本語スモークテスト結果",
        "",
    ]
    if has_run_id:
        lines.extend(
            [
                "| Run | モデル | 生成時間 | 応答文字数 | 日本語文字比率 | 観察メモ | 応答抜粋 |",
                "|---|---|---:|---:|---:|---|---|",
            ]
        )
    else:
        lines.extend(
            [
                "| モデル | 生成時間 | 応答文字数 | 日本語文字比率 | 観察メモ | 応答抜粋 |",
                "|---|---:|---:|---:|---|---|",
            ]
        )

    for row in rows:
        response = row.get("response", "")
        model = row.get("model_file", "")
        generate_ms = float(row.get("generate_ms") or 0)
        chars = int(row.get("response_chars") or len(response))
        ratio = jp_ratio(response)
        values = {
            "run": row.get("run_id", ""),
            "model": model,
            "sec": generate_ms / 1000,
            "chars": chars,
            "ratio": ratio,
            "obs": observation(response),
            "excerpt": compact(response, args.excerpt_chars).replace("|", "\\|"),
        }
        if has_run_id:
            lines.append(
                "| {run} | {model} | {sec:.2f}秒 | {chars} | {ratio:.2f} | {obs} | {excerpt} |".format(
                    **values
                )
            )
        else:
            lines.append(
                "| {model} | {sec:.2f}秒 | {chars} | {ratio:.2f} | {obs} | {excerpt} |".format(
                    **values
                )
            )

    lines.extend(
        [
            "",
            "## 見方",
            "",
            "- 日本語文字比率は、ひらがな、カタカナ、漢字が応答全体に占める割合です。",
            "- 観察メモは機械的な目安です。最終判断は応答本文を読んで行います。",
        ]
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Markdownを保存しました: {output_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Hailo-Ollama client utilities.

`hailo-ollama` の REST API を使うベンチマークスクリプト向けの共通処理を提供する。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List


@dataclass
class ChatResult:
    """1回のチャット推論の結果"""

    response_text: str
    total_ms: float
    ttft_ms: float
    eval_count: int
    eval_duration_ns: int
    prompt_eval_count: int
    prompt_eval_duration_ns: int
    tokens_per_sec: float
    done_reason: str
    raw_final_chunk: Dict[str, Any]


class HailoOllamaClient:
    """Hailo-Ollama APIクライアント"""

    def __init__(self, server_url: str = "http://localhost:8000", timeout: int = 180):
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout

    def list_models(self) -> List[str]:
        """`/hailo/v1/list` から利用可能モデル一覧を取得"""
        endpoint = f"{self.server_url}/hailo/v1/list"
        with urllib.request.urlopen(endpoint, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("models", [])

    def pull_model(self, model_name: str) -> Dict[str, Any]:
        """モデルを取得"""
        endpoint = f"{self.server_url}/api/pull"
        body = {"model": model_name, "stream": False}
        return self._post_json(endpoint, body)

    def chat(
        self,
        model_name: str,
        prompt: str,
        max_tokens: int = 128,
        temperature: float = 0.0,
    ) -> ChatResult:
        """ストリーミングでチャットを実行し、TTFTとTPSを計測"""
        endpoint = f"{self.server_url}/api/chat"
        body = {
            "model": model_name,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        return self._stream_request(request, mode="chat")

    def generate(
        self,
        model_name: str,
        prompt: str,
        max_tokens: int = 128,
        temperature: float = 0.0,
    ) -> ChatResult:
        """ストリーミングでテキスト生成を実行し、TTFTとTPSを計測"""
        endpoint = f"{self.server_url}/api/generate"
        body = {
            "model": model_name,
            "stream": True,
            "prompt": prompt,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        return self._stream_request(request, mode="generate")

    def _stream_request(self, request: urllib.request.Request, mode: str) -> ChatResult:
        """`/api/chat` と `/api/generate` のストリーム応答を共通形式へ変換する"""
        response_parts: List[str] = []
        final_chunk: Dict[str, Any] = {}
        first_token_at: float | None = None
        started_at = time.time()

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue

                    chunk = json.loads(line)
                    if mode == "generate":
                        content = chunk.get("response", "")
                    else:
                        message = chunk.get("message", {})
                        content = message.get("content", "")

                    if content:
                        if first_token_at is None:
                            first_token_at = time.time()
                        response_parts.append(content)

                    if chunk.get("done"):
                        final_chunk = chunk
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace").strip()
            if error_body:
                raise RuntimeError(
                    f"HTTPエラー: {exc.code} {exc.reason}\n{error_body}"
                ) from exc
            raise RuntimeError(f"HTTPエラー: {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"サーバー接続エラー: {exc.reason}") from exc

        finished_at = time.time()
        eval_count = int(final_chunk.get("eval_count", 0) or 0)
        eval_duration_ns = int(final_chunk.get("eval_duration", 0) or 0)
        prompt_eval_count = int(final_chunk.get("prompt_eval_count", 0) or 0)
        prompt_eval_duration_ns = int(final_chunk.get("prompt_eval_duration", 0) or 0)
        done_reason = str(final_chunk.get("done_reason", "") or "")
        tokens_per_sec = 0.0

        if eval_count > 0 and eval_duration_ns > 0:
            tokens_per_sec = eval_count / (eval_duration_ns / 1_000_000_000)
        elif eval_count > 0 and first_token_at is not None:
            generation_window_s = max(finished_at - first_token_at, 1e-9)
            tokens_per_sec = eval_count / generation_window_s

        return ChatResult(
            response_text="".join(response_parts),
            total_ms=(finished_at - started_at) * 1000,
            ttft_ms=((first_token_at - started_at) * 1000) if first_token_at else 0.0,
            eval_count=eval_count,
            eval_duration_ns=eval_duration_ns,
            prompt_eval_count=prompt_eval_count,
            prompt_eval_duration_ns=prompt_eval_duration_ns,
            tokens_per_sec=tokens_per_sec,
            done_reason=done_reason,
            raw_final_chunk=final_chunk,
        )

    def _post_json(self, endpoint: str, body: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def load_prompt_records(path: str) -> List[Dict[str, Any]]:
    """
    プロンプト定義を読み込む。

    `jsonl` では以下を想定する。
    - `id`
    - `category`
    - `prompt`
    - `expected_keywords` (任意)
    """
    prompt_path = Path(path)
    records: List[Dict[str, Any]] = []

    if prompt_path.suffix == ".jsonl":
        with prompt_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records

    with prompt_path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            prompt = line.strip()
            if not prompt:
                continue
            records.append(
                {
                    "id": f"prompt_{index:02d}",
                    "category": "general",
                    "prompt": prompt,
                    "expected_keywords": [],
                }
            )

    return records


def keyword_coverage(response_text: str, expected_keywords: Iterable[str]) -> float:
    """期待キーワードの一致率を返す"""
    keywords = [keyword for keyword in expected_keywords if keyword]
    if not keywords:
        return 0.0

    matches = sum(1 for keyword in keywords if keyword in response_text)
    return matches / len(keywords)

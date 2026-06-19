#!/usr/bin/env python3
"""
LLM Inference Benchmark - LLM推論性能測定スクリプト

このスクリプトは、Hailo NPUを使用したLLM（Granite 3.3:2b）の性能を測定します：
- トークン生成速度（tokens/sec）
- TTFT（Time To First Token）
- メモリ使用量
- スワップ発生回数
- 応答品質
"""

import argparse
import time
import sys
from pathlib import Path
import numpy as np

# 共通ユーティリティをインポート
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from benchmark_utils import SystemMonitor, DataSaver, PerformanceMetrics
except ModuleNotFoundError as exc:
    if exc.name == "benchmark_utils":
        print("エラー: benchmark_utils.py が見つかりません")
        print(f"期待する場所: {SCRIPT_DIR / 'benchmark_utils.py'}")
        print("scripts ディレクトリ一式を同じ場所に置いたまま実行してください")
        raise SystemExit(1)
    raise

try:
    import hailo_llm
except ImportError as e:
    print(f"警告: 必要なライブラリがインストールされていません: {e}")
    print("このスクリプトはRaspberry Pi上で実行してください")


class LLMBenchmark:
    """LLM推論のベンチマーククラス"""
    
    def __init__(self, model_path: str, max_tokens: int = 512):
        """
        初期化
        
        Args:
            model_path: Hailoモデルファイルのパス
            max_tokens: 最大トークン数
        """
        self.model_path = model_path
        self.max_tokens = max_tokens
        
        print(f"モデルをロード中: {model_path}")
        print(f"最大トークン数: {max_tokens}")
        
        # Hailo LLM Runtimeの初期化
        try:
            self.llm = hailo_llm.LLM(
                model_path=model_path,
                max_tokens=max_tokens,
                temperature=0.7
            )
            print("✓ Hailo LLM Runtimeの初期化完了")
        except Exception as e:
            print(f"✗ Hailo LLM Runtimeの初期化失敗: {e}")
            raise
        
        # プロンプトテンプレート
        self.prompt_template = """以下の質問に簡潔に答えてください。

質問: {question}
回答:"""
    
    def generate_response(self, question: str, max_new_tokens: int = 100) -> dict:
        """
        質問に対する回答を生成し、性能メトリクスを記録
        
        Args:
            question: 質問文
            max_new_tokens: 生成する最大トークン数
        
        Returns:
            dict: 生成結果と性能メトリクス
        """
        # システムリソース測定開始
        system_monitor = SystemMonitor()
        mem_before = system_monitor.measure()
        
        # プロンプト準備
        prompt = self.prompt_template.format(question=question)
        
        # 推論実行
        start_time = time.time()
        first_token_time = None
        tokens = []
        
        try:
            for token in self.llm.generate(
                prompt,
                max_new_tokens=max_new_tokens,
                stream=True
            ):
                if first_token_time is None:
                    first_token_time = time.time()
                tokens.append(token)
        except Exception as e:
            print(f"エラー: 生成中に問題が発生しました: {e}")
            return None
        
        end_time = time.time()
        
        # システムリソース測定終了
        mem_after = system_monitor.measure()
        
        # メトリクス計算
        total_time = end_time - start_time
        ttft = (first_token_time - start_time) * 1000 if first_token_time else 0  # ms
        tokens_per_sec = len(tokens) / total_time if total_time > 0 else 0
        
        result = {
            'question': question,
            'response': ''.join(tokens),
            'metrics': {
                'ttft_ms': ttft,
                'total_time_sec': total_time,
                'token_count': len(tokens),
                'tokens_per_sec': tokens_per_sec,
                'memory_before_mb': mem_before['memory_used_mb'],
                'memory_after_mb': mem_after['memory_used_mb'],
                'memory_delta_mb': mem_after['memory_used_mb'] - mem_before['memory_used_mb'],
                'swap_before_mb': mem_before['swap_used_mb'],
                'swap_after_mb': mem_after['swap_used_mb'],
                'swap_occurred': mem_after['swap_used_mb'] > mem_before['swap_used_mb']
            }
        }
        
        return result
    
    def run_benchmark(self, prompts: list, continuous: int = None) -> dict:
        """
        ベンチマークを実行
        
        Args:
            prompts: 質問のリスト
            continuous: 連続生成回数（Noneの場合はpromptsを1回実行）
        
        Returns:
            dict: ベンチマーク結果
        """
        print(f"\n=== LLMベンチマーク開始 ===\n")
        
        results = []
        system_monitor = SystemMonitor()
        
        # 連続生成モード
        if continuous:
            print(f"連続生成モード: {continuous}回")
            for i in range(continuous):
                # プロンプトをローテーション
                question = prompts[i % len(prompts)]
                
                print(f"\n[{i+1}/{continuous}] 質問: {question[:50]}...")
                
                result = self.generate_response(question)
                if result:
                    results.append(result)
                    metrics = result['metrics']
                    
                    print(f"  TTFT: {metrics['ttft_ms']:.1f}ms")
                    print(f"  トークン数: {metrics['token_count']}")
                    print(f"  速度: {metrics['tokens_per_sec']:.2f} tokens/sec")
                    print(f"  メモリ: {metrics['memory_after_mb']:.1f}MB "
                          f"(+{metrics['memory_delta_mb']:.1f}MB)")
                    if metrics['swap_occurred']:
                        print(f"  ⚠ スワップ発生")
                
                # システムリソース測定
                system_monitor.measure()
        
        # 通常モード
        else:
            print(f"通常モード: {len(prompts)}個の質問")
            for i, question in enumerate(prompts):
                print(f"\n[{i+1}/{len(prompts)}] 質問: {question[:50]}...")
                
                result = self.generate_response(question)
                if result:
                    results.append(result)
                    metrics = result['metrics']
                    
                    print(f"  TTFT: {metrics['ttft_ms']:.1f}ms")
                    print(f"  トークン数: {metrics['token_count']}")
                    print(f"  速度: {metrics['tokens_per_sec']:.2f} tokens/sec")
                    print(f"  回答: {result['response'][:100]}...")
                
                # システムリソース測定
                system_monitor.measure()
        
        # 結果の集計
        if not results:
            print("\n警告: 有効な結果がありません")
            return {}
        
        ttfts = [r['metrics']['ttft_ms'] for r in results]
        tokens_per_secs = [r['metrics']['tokens_per_sec'] for r in results]
        token_counts = [r['metrics']['token_count'] for r in results]
        memory_deltas = [r['metrics']['memory_delta_mb'] for r in results]
        swap_occurrences = sum(1 for r in results if r['metrics']['swap_occurred'])
        
        system_stats = system_monitor.get_statistics()
        
        summary = {
            'total_queries': len(results),
            'ttft': {
                'mean_ms': np.mean(ttfts),
                'std_ms': np.std(ttfts),
                'min_ms': np.min(ttfts),
                'max_ms': np.max(ttfts),
                'p95_ms': np.percentile(ttfts, 95)
            },
            'throughput': {
                'mean_tokens_per_sec': np.mean(tokens_per_secs),
                'std_tokens_per_sec': np.std(tokens_per_secs),
                'min_tokens_per_sec': np.min(tokens_per_secs),
                'max_tokens_per_sec': np.max(tokens_per_secs)
            },
            'tokens': {
                'mean_count': np.mean(token_counts),
                'total_count': sum(token_counts)
            },
            'memory': {
                'mean_delta_mb': np.mean(memory_deltas),
                'max_delta_mb': np.max(memory_deltas)
            },
            'stability': {
                'swap_occurrences': swap_occurrences,
                'swap_rate': swap_occurrences / len(results)
            },
            'system': system_stats
        }
        
        # 結果表示
        print(f"\n=== ベンチマーク結果 ===")
        print(f"総クエリ数: {summary['total_queries']}")
        print(f"平均TTFT: {summary['ttft']['mean_ms']:.2f}ms")
        print(f"P95 TTFT: {summary['ttft']['p95_ms']:.2f}ms")
        print(f"平均トークン生成速度: {summary['throughput']['mean_tokens_per_sec']:.2f} tokens/sec")
        print(f"平均トークン数: {summary['tokens']['mean_count']:.1f}")
        print(f"総トークン数: {summary['tokens']['total_count']}")
        print(f"スワップ発生回数: {summary['stability']['swap_occurrences']} "
              f"({summary['stability']['swap_rate']*100:.1f}%)")
        print(f"平均CPU使用率: {summary['system']['cpu_percent']['mean']:.1f}%")
        print(f"平均メモリ使用率: {summary['system']['memory_percent']['mean']:.1f}%")
        
        return {
            'summary': summary,
            'details': results
        }


def load_prompts_from_file(filepath: str) -> list:
    """
    ファイルからプロンプトを読み込む
    
    Args:
        filepath: プロンプトファイルのパス
    
    Returns:
        list: プロンプトのリスト
    """
    prompts = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                prompts.append(line)
    return prompts


def main():
    """メイン関数"""
    parser = argparse.ArgumentParser(
        description='LLM推論の性能測定スクリプト'
    )
    parser.add_argument(
        '--model',
        type=str,
        default='granite-3.3-2b_4bit_hailo.hef',
        help='Hailoモデルファイルのパス'
    )
    parser.add_argument(
        '--prompts',
        type=str,
        required=True,
        help='プロンプトファイルのパス'
    )
    parser.add_argument(
        '--max-tokens',
        type=int,
        default=512,
        help='最大トークン数'
    )
    parser.add_argument(
        '--continuous',
        type=int,
        default=None,
        help='連続生成回数'
    )
    parser.add_argument(
        '--output',
        type=str,
        default=None,
        help='結果の保存先（CSVファイル）'
    )
    parser.add_argument(
        '--evaluate',
        action='store_true',
        help='品質評価を実施'
    )
    
    args = parser.parse_args()
    
    # プロンプトの読み込み
    try:
        prompts = load_prompts_from_file(args.prompts)
        print(f"プロンプトを読み込みました: {len(prompts)}個")
    except Exception as e:
        print(f"エラー: プロンプトファイルの読み込みに失敗しました: {e}")
        sys.exit(1)
    
    if not prompts:
        print("エラー: プロンプトが空です")
        sys.exit(1)
    
    # ベンチマーク実行
    try:
        benchmark = LLMBenchmark(args.model, args.max_tokens)
        results = benchmark.run_benchmark(prompts, args.continuous)
        
        # 結果の保存
        if args.output and results:
            # サマリーをCSVに保存
            summary_data = [{
                'timestamp': time.time(),
                **results['summary'],
                **{f'ttft_{k}': v for k, v in results['summary']['ttft'].items()},
                **{f'throughput_{k}': v for k, v in results['summary']['throughput'].items()},
                **{f'tokens_{k}': v for k, v in results['summary']['tokens'].items()},
                **{f'memory_{k}': v for k, v in results['summary']['memory'].items()},
                **{f'stability_{k}': v for k, v in results['summary']['stability'].items()}
            }]
            DataSaver.save_to_csv(summary_data, args.output)
            
            # 詳細データをJSONに保存
            json_path = args.output.replace('.csv', '_detail.json')
            DataSaver.save_to_json(results, json_path)
        
    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

# Made with Bob

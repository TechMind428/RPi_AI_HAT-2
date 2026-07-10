# RPi_AI_HAT-2

本リポジトリは、Interface 2026年10月号 特集第3部の記事で使用するサンプルコードと測定結果をまとめたものです。

記事の第3章で案内している通り、Raspberry Pi側で本リポジトリを `git clone` し、`scripts/` 配下のファイルを `~/scripts/` にコピーして使うことを前提にしています。

## 使い方

まず、誌面の第3章に沿って Raspberry Pi OS、HailoRT、hailo-ollama、Python環境をセットアップしてください。その後、Raspberry Pi上で次のように取得します。

```bash
cd ~
git clone https://github.com/TechMind428/RPi_AI_HAT-2.git
mkdir -p ~/scripts
cp ~/RPi_AI_HAT-2/scripts/* ~/scripts/
chmod +x ~/scripts/*.sh
```

以降の章では、`~/scripts/` に置いたスクリプトを使って、YOLO物体検出、LLM比較、マルチモーダル実験を行います。

## ディレクトリ構成

- `scripts/`: 記事で使用するPythonスクリプト、実行用シェル、プロンプト定義
- `results/`: 記事で参照する測定結果
- `experiments/`: 測定CSVの列説明などの補足資料

## 含めていないもの

本リポジトリには、HailoのDebianパッケージ、Python wheel、HEFモデルファイルは含めていません。これらはサイズや配布条件の都合があるため、誌面の手順に従って公式配布元またはaptから取得してください。

## 注意

このリポジトリ単体でセットアップ手順を完結させるものではありません。必ず Interface 2026年10月号 特集第3部、特に第3章の手順とあわせて利用してください。

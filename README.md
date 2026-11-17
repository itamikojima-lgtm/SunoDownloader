# Suno Downloader Web v0.1.0

React＋TypeScript＋Tailwind CSSのスマホ向け画面と、既存Suno Downloader Python処理をつないだ単曲ダウンロードアプリです。URL入力 → 処理進捗・中止 → 保存の3状態を切り替えます。ライブラリ・履歴・ログイン認証情報の収集・プレイリスト取得機能はありません。

## 起動（Windows）

1. ZIPを展開します。
2. Python3.12（Python Launcherを含む）とFFmpegをインストールします。`ffmpeg`と`ffprobe`をPATHに追加します。
3. `setup_windows.bat`を実行します。初回のみインターネット接続が必要です。
4. `start_windows.bat`で起動します。PCブラウザの `http://127.0.0.1:8766` が開きます。
5. 終了はサーバー画面でCtrl+C。

ビルド済み画面を同梱しているので通常利用にNode.jsは不要です。Windows実機・スマホ実機は未検証です。ローカル起動はPC内限定です。スマホでの利用には以下のクラウド配置を行います。

## Renderへ配置する

本成果物は設定ファイルを含むソースです。Renderへの配置はまだ行っていません。

1. 展開したフォルダ内容を自分のGitHubリポジトリへ登録します（README/Dockerfileがルート）。
2. RenderでNew Web Service → GitHubリポジトリ → Docker → Freeを選択します。`render.yaml`によるBlueprintも利用できます。
3. Health Check Pathは`/api/health`。`COOKIE_SECURE=1`を設定します。DockerfileはPythonとFFmpegを導入し、画面をビルドして同じOriginから配信します。
4. 公開URLをスマホから開きます。Renderの`RENDER_EXTERNAL_URL`をOrigin検証に使います。独自ドメインの場合は`PUBLIC_ORIGIN=https://自分のドメイン`を追加します（末尾のパスなし）。
5. 1曲を取得して実端末で保存を確認します。ダウンロード権限がある曲を使用してください。

無料Renderは15分無通信で休止し、起動・再配置で結果が消えます。画面を開き直して同じブラウザタブに戻るとジョブを再照会しますが、サーバーの停止中に処理継続は保証しません。消えた場合は再実行します。サーバー側の準備完了と端末への保存完了は別です。

## 操作

- Sunoの `/s/…` または `/song/UUID` の共有URLを貼り付けます。
- MP3・M4A・WAVを選び開始。M4Aは元の音声コーデックを保持します。再生互換性を優先する場合はMP3を選んでください。現在の工程を表示し、割合が取得できる工程のみ進捗を表示します。
- 完了後「スマホに保存」を押します。実際の保存先・ファイルを開く動作はOSとブラウザで異なります。
- 中止ボタンはサーバー処理と関連FFmpeg子プロセスを停止します。
- 音源はサーバーへ一時保存され、完了・失敗・中止から15分で削除されます。ダウンロード中のファイルは配信終了まで保持します。サーバー再起動時は結果とセッションが無効になります。

## 制約と検証の区別

既存の配信先・権利エンドポイント依存のため、Suno側の変更・権利応答・接続拒否で取得に失敗する場合があります。拒否を回避する機能はありません。実Suno共有URL1曲の取得からM4A保存までをデスクトップChromeで確認しました。全曲・全形式・スマホ実機での成功を保証するものではありません。オフライン暗号化テスト素材では復号・3形式変換・HTTP保存を確認しています。

同時処理はサーバー全体で1曲、一時ジョブ最大8件、処理上限10分、入力音源128MB、出力256MiB、ジョブ内ファイル合計512MiBです。大きなWAVや長い曲は制限で失敗する場合があります。重い処理は別プロセスで実行します。FFmpegはCPU実行です。

匿名ブラウザセッションごとに結果を分離し、Cookie（HttpOnly/SameSite）とCSRFトークンで書き込みを保護します。一般公開サービス向けのアカウント制・課金・強固なアクセス制限は含みません。サーバー全体の同時処理制限があるので、不特定多数向けの運用では認証とレート制限を追加してください。個人利用の試作を対象とします。

## 開発

採用バージョン: Python3.12、Node22（Dockerビルド）、React19.1、TypeScript5.8、Tailwind4.1。詳細な固定値はロックファイル・requirementsを参照。

```bash
python -m pip install -r requirements-dev.txt
python -m pytest backend/tests -q
cd frontend
npm ci
npm run lint
npm test
npm run build
cd ..
python launch.py
```

PythonサーバーはUvicorn1ワーカー限定です。複数ワーカーはインメモリのジョブ・セッション管理に対応しません。

- `frontend/`: 実際にコンパイルするReact＋Tailwind画面
- `backend/app.py`: セッション、ジョブ、中止、保存、一時ファイル管理
- `backend/worker.py`: 隔離した既存Python取得処理
- `backend/vendor/`: 元ZIPからの流用コード
- `docs/REVIEW.md`: 独立レビュー
- `docs/VERIFICATION.md`: 実施した確認と未検証事項

## 元コードの流用

提供された `Suno_Downloader_v0_8_1_Long_Export_Diagnostic(1).zip` の `suno_poc.py`、`mango_engine.py`、`phase_a3.py`、`phase_b.py` を流用。UIは新規実装。phase_bのリトライで前回エラーが残る問題を修正し、取得サイズを128MBに制限しました。公開配信・再配布時は元コードと依存ソフトの権利・ライセンスを確認してください。

参考公式資料（2026-11-17確認）:
- https://tailwindcss.com/docs/installation/using-vite
- https://fastapi.tiangolo.com/advanced/response-cookies/
- https://render.com/docs/docker
- https://render.com/docs/web-services
- https://render.com/docs/free

# discord_local_player

ローカル PC 上の音源(mp3 / wav / ogg / flac / m4a など)を Discord のボイスチャンネルで再生する Bot です。
Windows 11 上で Bot を起動している間、その PC の `sounds/` フォルダにある音源を再生できます。

## 必要なもの

- Python 3.12 以上
- FFmpeg(音声のデコードに使用)
- Discord Bot トークン

## セットアップ

### 1. Python と FFmpeg のインストール

```powershell
winget install Python.Python.3.13
winget install Gyan.FFmpeg
```

インストール後、ターミナルを開き直して `python --version` と `ffmpeg -version` が通ることを確認してください。

### 2. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) で New Application を作成
2. Bot タブで **Reset Token** を押してトークンを取得(他人に見せないこと)
3. OAuth2 → URL Generator で `bot` と `applications.commands` を選択し、
   Bot Permissions は **Connect** と **Speak** を付けて、生成された URL からサーバーに招待

特権インテントの有効化は不要です。

### 3. 依存パッケージと設定

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

`.env` を開いて `DISCORD_TOKEN` を設定します。
`GUILD_ID` にサーバー ID を入れておくと、スラッシュコマンドがすぐ反映されます。

### 4. 音源を置いて起動

`sounds/` に音源ファイルを入れて、次を実行します。

```powershell
python bot.py
```

## コマンド

| コマンド | 内容 |
| --- | --- |
| `/join` | 自分のいるボイスチャンネルに参加 |
| `/leave` | 退出(キューも破棄) |
| `/play name` | `sounds/` 内の音源を再生。再生中ならキューに追加(入力中にファイル名が補完される) |
| `/skip` | 再生中の音源をスキップして次へ |
| `/stop` | 停止してキューを空にする |
| `/pause` / `/resume` | 一時停止 / 再開 |
| `/queue` | 再生中の音源とキューを表示 |
| `/remove index` | キューの指定番号を削除 |
| `/clear` | キューを空にする(再生中の音源はそのまま) |
| `/shuffle` | キューをシャッフル |
| `/loop mode` | リピート設定(オフ / 1曲リピート / キュー全体リピート) |
| `/volume percent` | 音量 0-100(初期値 50) |
| `/list` | 再生可能な音源の一覧 |

`/play` はボイスチャンネル未参加でも、コマンドを打った人のチャンネルに自動で参加します。
サブフォルダ内の音源は `bgm/battle.mp3` のように指定します。
キューは Bot を再起動すると消えます(最大 100 件)。

# RGA Assistant

Ticket Tool連携のチケットリマインダーBotです。設定したDiscordカテゴリ内の
チャンネルが作成されると、その作成日時を起点にリマインダーを登録し、設定した
時間が経過した後にチケットチャンネルや運営用チャンネルへ1回送信します。
未送信のリマインダーはSQLiteに保存され、Bot再起動後も引き継がれます。

## セットアップ

1. Python 3.10以降をインストールします。
2. 仮想環境を作成・有効化し、`pip install -r requirements.txt` を実行します。
3. `.env.example` を `.env` にコピーし、環境変数を設定します（次節参照）。
4. 監視対象カテゴリでBotに**チャンネルを見る**・**メッセージを送信**権限を
   付与します。運営用チャンネルへ送信する場合も同様に権限を付与します。
5. [Discord Developer Portal](https://discord.com/developers/applications)で対象アプリを開き、
   **Bot** > **Privileged Gateway Intents** > **Message Content Intent**を有効にします。
6. リポジトリ直下で `python main.py` を実行します。

Message Content Intentを有効にせずに起動すると、Discordが接続を拒否します。
有効化後も同じエラーが出る場合は、Botトークンが対象アプリのものか確認してから
Botを再起動してください。

## 環境変数

| 変数 | 必須 | 既定値 | 説明 |
|---|---|---|---|
| `DISCORD_TOKEN` | ○ | — | Botのトークン。 |
| `RGA_TICKET_CATEGORY_IDS` | ○ | — | 監視するDiscordカテゴリID（カンマ区切り）。 |
| `RGA_EXCLUDED_CHANNEL_IDS` | | 空 | 監視対象から除外するチャンネルID（カンマ区切り）。 |
| `RGA_REMINDER_TRIGGER_TEXT` | ○ | — | 未送信リマインダーをキャンセルする完全一致のメッセージ本文。 |
| `RGA_REMINDER_VALUE` | | `14` | リマインダーまでの時間。 |
| `RGA_REMINDER_UNIT` | | `days` | `seconds`/`minutes`/`hours`/`days`/`weeks`。 |
| `RGA_REMINDER_TARGET` | | `ticket` | 送信先。`ticket`/`operations`/`both`。 |
| `RGA_OPERATIONS_CHANNEL_ID` | △ | 空 | 運営用チャンネルID。`operations`/`both`では必須。失敗通知先も兼ねます。 |
| `RGA_OPERATIONS_MENTION_ROLE_ID` | | 空 | 運営向け通知でメンションするロールID。 |
| `RGA_REOPEN_TRIGGER_TEXT` | | 空 | 再オープンとして扱う完全一致のメッセージ本文。空なら無効。 |
| `RGA_RETRY_BASE_SECONDS` | | `60` | 送信失敗時の再試行間隔の基準（秒）。 |
| `RGA_RETRY_MAX_SECONDS` | | `21600` | 再試行間隔の上限（秒、既定6時間）。 |
| `RGA_RETRY_MAX_ATTEMPTS` | | `6` | この回数失敗すると`error`状態になります。 |
| `RGA_DATABASE_PATH` | | `data/rga_assistant.sqlite3` | SQLiteデータベースのパス。 |

旧設定 `RGA_TICKET_CATEGORY_ID`（単一カテゴリ）と `RGA_REMINDER_DAYS` も
引き続き利用できます。

## 動作

### リマインダーの登録

設定したカテゴリ内のテキストチャンネルについて、作成日時を起点にリマインダーを
自動登録します。登録のきっかけは次の3つです。

- Bot起動時の監視対象チャンネルスキャン
- 監視カテゴリ内でのチャンネル作成
- チャンネルが監視カテゴリへ移動したとき

定期再スキャンは行いません（`60秒ごとの再照合`は実装していません）。Bot停止中に
作成されたチャンネルは、次回起動時のスキャンで登録されます。

予定日時はチャンネル作成日時＋設定時間（既定14日）です。日時はUTCで保存し、
表示時に日本時間（JST）へ変換します。同じチャンネルを再確認しても既存の予定は
リセットされません。

### 送信先

`RGA_REMINDER_TARGET` で切り替えます。

- `ticket`（既定）：チケットチャンネルへ、申請者向けの文面で送信します。
- `operations`：運営用チャンネルへ、チケットへのリンクを含む運営向けの文面で送信します。
- `both`：両方へ、それぞれ別の文面で送信します。片方だけ成功しても送信済みの
  分は再送されません。

送信先の設定は**予定の登録時点**で確定します。`RGA_REMINDER_TARGET` を変更すると、
以後に登録されるチケットから新しい設定が適用されます。

### キャンセルと再オープン

`RGA_REMINDER_TRIGGER_TEXT` と完全に一致するメッセージが投稿されると、その
チャンネルの未送信リマインダーをキャンセルします。大文字・小文字は区別し、
部分一致ではキャンセルしません。

`RGA_REOPEN_TRIGGER_TEXT` を設定すると、その本文と完全一致するメッセージで
再オープンとして扱い、**その日時を起点に新しい14日間**で予定を組み直します。

チャンネルが削除された場合は、その未送信リマインダーをキャンセルします。

### リマインダーの送信と再試行

予定の確認は5秒間隔で行うため、予定時刻から最大約5秒後に送信されます。

送信に失敗した場合は指数バックオフで再試行します。間隔は
`RGA_RETRY_BASE_SECONDS × 2^(失敗回数-1)` を `RGA_RETRY_MAX_SECONDS` で頭打ちに
した値です。`RGA_RETRY_MAX_ATTEMPTS` 回失敗すると `error` 状態で停止し、
ログに記録します。`RGA_OPERATIONS_CHANNEL_ID` が設定されていれば、運営用
チャンネルへも失敗を通知します。

権限不足（`Forbidden`）や送信先の不在など、再試行しても成功しないと判断できる
失敗は、バックオフせず即座に `error` 状態にします。チャンネルが取得できず
`NotFound` の場合は、チケットが閉じたとみなしてキャンセルします。

## 送信失敗からの復旧

`error` 状態の送信は、次のコマンドで再試行対象に戻せます。リポジトリ直下で
実行してください。

```sh
python -m tools.retry            # すべての失敗を再試行対象に戻す
python -m tools.retry 1234567890 # 指定チャンネルのみ
python -m tools.retry --list     # 失敗状態を一覧表示
```

## 通知先チャンネルの設定

運営用チャンネルへ送信する場合は、`RGA_OPERATIONS_CHANNEL_ID` にチャンネルIDを
設定し、`RGA_REMINDER_TARGET` を `operations` または `both` にします。
ロールをメンションする場合は `RGA_OPERATIONS_MENTION_ROLE_ID` にロールIDを
設定します。メンションは運営向け通知にのみ付与されます。

## Ticket Toolとの連携設定

現状はTicket Toolの公式Webhook/APIを使わず、Discordの**チャンネル作成・更新
イベント**でチケット開設を検知する方式です（Ticket ToolのWebhook/APIはPro
プラン以上の機能のため）。

クローズ検知は `RGA_REMINDER_TRIGGER_TEXT` による完全一致に依存します。
Ticket Toolのクローズ時メッセージ（カスタムメッセージ設定を含む）と本文が
一致するように設定してください。文面を変更するとキャンセルが効かなくなる
点に注意してください。

## systemdによる運用

`deploy/rga-assistant.service` を用意しています。パス・ユーザーは環境に合わせて
編集してください。

```sh
sudo cp deploy/rga-assistant.service /etc/systemd/system/rga-assistant.service
sudo systemctl daemon-reload
sudo systemctl enable --now rga-assistant
```

停止・再起動・状態確認:

```sh
sudo systemctl stop rga-assistant
sudo systemctl restart rga-assistant
sudo systemctl status rga-assistant
```

`Restart=always` を設定しているため、異常終了時は自動で再起動します。

## ログの確認

systemdで運用している場合:

```sh
journalctl -u rga-assistant -f          # 追従表示
journalctl -u rga-assistant --since today
```

`python main.py` で直接起動している場合は、標準出力・標準エラーに出力されます。

## データベース

SQLite（既定 `data/rga_assistant.sqlite3`）に予定と送信状態を保存します。
バックアップはBotを停止した状態でファイルをコピーするか、`sqlite3` の
`.backup` を利用してください。

```sh
sqlite3 data/rga_assistant.sqlite3 ".backup 'backup.sqlite3'"
```

スキーマは `PRAGMA user_version` で管理し、起動時に必要な移行を自動適用します。

## よくあるエラーと対処

| 症状 | 対処 |
|---|---|
| 起動時に `Message Content Intent を有効にしてください。` | Developer Portalで Message Content Intent を有効化し、再起動します。 |
| `RGA_TICKET_CATEGORY_IDS ... must contain valid values.` | カテゴリIDが正の整数のカンマ区切りか確認します。 |
| `RGA_OPERATIONS_CHANNEL_ID is required ...` | `RGA_REMINDER_TARGET` が `operations`/`both` のときは運営チャンネルIDを設定します。 |
| リマインダーが送信されない | Botに「チャンネルを見る」「メッセージを送信」権限があるか確認します。`error` 状態なら `python -m tools.retry` で復旧します。 |
| クローズしたのに通知が来る | `RGA_REMINDER_TRIGGER_TEXT` がクローズ時メッセージと完全一致しているか確認します。 |

## テスト

```sh
python -m unittest discover -s tests
```

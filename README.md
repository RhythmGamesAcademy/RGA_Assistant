# RGA Assistant

Ticket Tool連携のチケットリマインダーと講義スケジュール通知を行うDiscord Botです。
設定したDiscordカテゴリ内のチャンネルが作成されると、その作成日時を起点に
リマインダーを登録し、設定した時間が経過した後にチケットチャンネルや運営用
チャンネルへ1回送信します。講義予定はスラッシュコマンドから登録でき、開始前に
案内チャンネルへリマインダーを送信します。予定はPostgreSQLに保存され、Bot再起動後も
引き継がれます。

## セットアップ

1. Python 3.10以降をインストールします。
2. PostgreSQL 12以降を用意し、Bot用のデータベースとユーザーを作成します。
3. 仮想環境を作成・有効化し、`pip install -r requirements.txt` を実行します。
4. `.env.example` を `.env` にコピーし、環境変数を設定します（次節参照）。
   `RGA_DATABASE_URL` にはPostgreSQLの接続文字列を指定します。
5. 監視対象カテゴリでBotに**チャンネルを見る**・**メッセージを送信**・
   **リンクを埋め込む**権限を付与します。運営用チャンネルへ送信する場合も同様に
   権限を付与します。講義案内を投稿するチャンネルにも同じ権限が必要です。
6. Botを招待する際は `bot` と `applications.commands` の両方のスコープを指定します
   （スラッシュコマンドの登録に必要です）。
7. [Discord Developer Portal](https://discord.com/developers/applications)で対象アプリを開き、
   **Bot** > **Privileged Gateway Intents** > **Message Content Intent**を有効にします。
8. リポジトリ直下で `python main.py` を実行します。

テーブルは起動時に自動作成されます。事前に手動でスキーマを流す必要はありません。

Message Content Intentを有効にせずに起動すると、Discordが接続を拒否します。
有効化後も同じエラーが出る場合は、Botトークンが対象アプリのものか確認してから
Botを再起動してください。

## 環境変数

| 変数 | 必須 | 既定値 | 説明 |
|---|---|---|---|
| `DISCORD_TOKEN` | ○ | — | Botのトークン。 |
| `RGA_TICKET_CATEGORY_IDS` | ○ | — | 監視するDiscordカテゴリID（カンマ区切り）。 |
| `RGA_EXCLUDED_CHANNEL_IDS` | | 空 | 監視対象から除外するチャンネルID（カンマ区切り）。 |
| `RGA_REMINDER_CANCEL_TRIGGER` | | `image` | 未送信リマインダーのキャンセル条件。`image` / `keyword` から選択します（「キャンセル」節参照）。 |
| `RGA_REMINDER_TRIGGER_TEXT` | △ | — | `RGA_REMINDER_CANCEL_TRIGGER` が `keyword` のときのみ必須。未送信リマインダーをキャンセルするメッセージ本文。カンマ区切りで複数指定できます。 |
| `RGA_REMINDER_VALUE` | | `14` | リマインダーまでの時間。 |
| `RGA_REMINDER_UNIT` | | `days` | `seconds`/`minutes`/`hours`/`days`/`weeks`。 |
| `RGA_REMINDER_TARGET` | | `ticket` | 送信先。`ticket`/`operations`/`both`。 |
| `RGA_OPERATIONS_CHANNEL_ID` | △ | 空 | 運営用チャンネルID。`operations`/`both`では必須。失敗通知先も兼ねます。 |
| `RGA_OPERATIONS_MENTION_ROLE_ID` | | 空 | 運営向け通知でメンションするロールID。 |
| `RGA_OPERATIONS_MENTION_USER_ID` | | 空 | 運営向け通知でメンションするユーザーID。`RGA_OPERATIONS_MENTION_ROLE_ID` とは併用できません。 |
| `RGA_RETRY_BASE_SECONDS` | | `60` | 送信失敗時の再試行間隔の基準（秒）。 |
| `RGA_RETRY_MAX_SECONDS` | | `21600` | 再試行間隔の上限（秒、既定6時間）。 |
| `RGA_RETRY_MAX_ATTEMPTS` | | `6` | この回数失敗すると`error`状態になります。 |
| `RGA_RETENTION_DAYS` | | `90` | 終了済みデータの保持日数。`python -m tools.purge` が使用します。 |
| `RGA_DATABASE_URL` | ○ | — | PostgreSQLの接続文字列（例: `postgresql://rga:password@localhost:5432/rga_assistant`）。 |
| `RGA_LECTURE_ROLE_IDS` | | 空 | 講義コマンドを実行できるロールID（カンマ区切り）。空の場合は管理者のみ。 |
| `RGA_LECTURE_REMINDER_MINUTES` | | `15` | 講義開始の何分前にリマインダーを送るか。 |
| `RGA_LECTURE_ANNOUNCE_CHANNEL_ID` | | 空 | 講義案内の既定投稿先チャンネル。`/lecture create` で個別指定も可能。 |

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

### キャンセル

`RGA_REMINDER_CANCEL_TRIGGER` でキャンセル条件を選びます（併用はできません）。

- `image`（既定）：監視カテゴリ内のチケットチャンネルに、**画像ファイルを添付した投稿**
  があると、そのチャンネルの未送信リマインダーをキャンセルします。
  （PNG/JPEG/GIF/WebPなど、添付ファイルのMIMEタイプが `image/*` のもの）
- `keyword`：`RGA_REMINDER_TRIGGER_TEXT` に列挙したキーワードのいずれかと完全に一致する
  メッセージが投稿されると、そのチャンネルの未送信リマインダーをキャンセルします
  （従来の動作）。このとき `RGA_REMINDER_TRIGGER_TEXT` は必須です。キーワードは
  カンマ区切りで複数指定でき、それぞれ**完全一致**で判定します。大文字・小文字は
  区別し、部分一致ではキャンセルしません（例: `終了, End`）。

共通の注意点:

- BotやWebhookが投稿したメッセージはキャンセル判定の対象外です。申請者本人の
  投稿のみが対象です。
- 画像URLを貼り付けた際に生成される埋め込みプレビューは、添付ファイルではないため
  対象外です。
- 設定値はモード変更後の判定から適用されます。既に登録済みの予定も、以後の判定では
  新しい設定が使われます。

再オープンは扱いません。閉じたチケットで再度やり取りしたい場合は、新しい
チケットを開いてもらう運用とします。

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

`error` 状態の送信は、Discordから次のコマンドで確認・再試行できます
（管理者または `RGA_LECTURE_ROLE_IDS` のロール保持者）。

- `/rga failures`：失敗状態の送信を一覧表示します。
- `/rga retry`：失敗状態の送信を再試行対象に戻します。`channel_id` を指定すると
  そのチャンネルだけを対象にします。

サーバー外（リポジトリ直下のCLI）から操作する場合は次のコマンドを使います。

```sh
python -m tools.retry            # すべての失敗を再試行対象に戻す
python -m tools.retry 1234567890 # 指定チャンネルのみ
python -m tools.retry --list     # 失敗状態を一覧表示
```

旧SQLiteデータの取り込みは `python -m tools.migrate_sqlite` で行います（「データベース」
の節を参照）。

## 講義スケジュール

`/lecture` コマンドで講義予定を登録し、開始前にリマインダーを送信します。
コマンドは起動時にグローバル同期されるため、反映まで数分かかることがあります。

### コマンド

| コマンド | 用途 | 実行権限 |
|---|---|---|
| `/lecture create` | 講義予定を登録し、案内チャンネルへ自動投稿します。 | `RGA_LECTURE_ROLE_IDS` のロール保持者または管理者 |
| `/lecture edit` | 講義予定を変更し、投稿済みメッセージを編集して変更通知を投稿します。 | 同上 |
| `/lecture cancel` | 講義予定を中止し、投稿済みメッセージを中止表示に更新して通知します。 | 同上 |
| `/lecture list` | 講義予定の一覧を表示します。 | 全利用者 |
| `/rga help` | Botの機能とコマンドの説明を表示します。 | 全利用者 |
| `/rga failures` | 送信に失敗したチケットリマインダーを一覧表示します。 | `RGA_LECTURE_ROLE_IDS` のロール保持者または管理者 |
| `/rga retry` | 失敗した送信を再試行対象に戻します。 | 同上 |

`RGA_LECTURE_ROLE_IDS` が空の場合、`create` / `edit` / `cancel` は管理者のみ
実行できます。

### 登録項目

講義名、開始日時（日本時間、`YYYY/MM/DD HH:MM`）、講師名、説明（任意）、
投稿先チャンネル（未指定なら `RGA_LECTURE_ANNOUNCE_CHANNEL_ID`）です。

登録時は案内メッセージを投稿先へ送信し、`/lecture list` で確認できる10文字の
講義IDを返します。

### リマインダー

開始の `RGA_LECTURE_REMINDER_MINUTES` 分前（既定15分前）に、登録した投稿先
チャンネルへリマインダーを送信します。送信状態はPostgreSQLに保存され、Bot再起動後も
復元されます。予定の確認は30秒間隔で行うため、送信は最大約30秒遅れます。

- 講義の開始時刻を過ぎた未送信リマインダーは送信せず、期限切れとして記録します。
- 送信に失敗した場合は1分間隔で開始時刻まで再試行します。権限不足やチャンネル
  不在など再試行しても成功しない失敗は期限切れにし、ログへ記録します。
- 変更時は投稿済みの案内メッセージ（送信済みリマインダーを含む）を編集し、
  変更通知を投稿先へ送信します。中止時は中止表示に更新し、中止通知を投稿します。

## 通知先チャンネルの設定

運営用チャンネルへ送信する場合は、`RGA_OPERATIONS_CHANNEL_ID` にチャンネルIDを
設定し、`RGA_REMINDER_TARGET` を `operations` または `both` にします。
メンションする場合は `RGA_OPERATIONS_MENTION_ROLE_ID`（ロール）または
`RGA_OPERATIONS_MENTION_USER_ID`（ユーザー）のどちらかを設定します。両方は
設定できません。メンションは運営向け通知にのみ付与されます。

## Ticket Toolとの連携設定

現状はTicket Toolの公式Webhook/APIを使わず、Discordの**チャンネル作成・更新
イベント**でチケット開設を検知する方式です（Ticket ToolのWebhook/APIはPro
プラン以上の機能のため）。

キャンセル条件は `RGA_REMINDER_CANCEL_TRIGGER` の設定に従います。`image`
（既定）では、申請完了の合図として申請者がチケットチャンネルへ画像を**ファイル添付**
してもらう運用になります（URLの貼り付けやBotの投稿は対象外です）。`keyword` では、
申請完了時に申請者へ送ってもらうメッセージ本文と `RGA_REMINDER_TRIGGER_TEXT` の
キーワードが**完全一致**するようにしてください。文面が複数ある場合はカンマ区切りで
すべて指定してください。文面を変更するとキャンセルが効かなくなる点に注意して
ください。

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

PostgreSQL（`RGA_DATABASE_URL`）にチケットの予定・送信状態と講義予定を保存します。
接続にはpsycopg3の非同期接続プールを使用します。

バックアップは `pg_dump` を利用してください。

```sh
pg_dump --format=custom "$RGA_DATABASE_URL" > backup.dump
```

### バックアップからの復旧

障害や誤操作でデータを失った場合は、`pg_restore` で復旧します。**Botを停止してから**
実行してください（起動中に書き込むと状態が競合します）。

```sh
sudo systemctl stop rga-assistant

# 既存の中身を空にしてから復元する（別DBに復旧する場合は不要）
psql "$RGA_DATABASE_URL" -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'

# custom形式のダンプを復元する
pg_restore --no-owner --no-privileges --dbname="$RGA_DATABASE_URL" backup.dump

sudo systemctl start rga-assistant
```

復旧後の確認手順:

1. `journalctl -u rga-assistant --since "5 min ago"` で起動ログにエラーがないか確認します。
2. `psql "$RGA_DATABASE_URL" -c "SELECT version FROM schema_version"` でスキーマ
   バージョンを確認します（起動時に自動で最新へ移行されます）。
3. `psql "$RGA_DATABASE_URL" -c "SELECT status, count(*) FROM ticket_reminders GROUP BY status"`
   で予定・送信済み・キャンセルの件数が想定どおりか確認します。
4. `python -m tools.retry --list` で失敗状態が残っていないか確認します（残っていれば
   `/rga retry` または `python -m tools.retry` で戻せます）。

### 保存期間と削除

終了したデータは自動では削除されないため、`python -m tools.purge` で定期的に
削除します。保持期間は `RGA_RETENTION_DAYS`（既定90日）で、更新からその日数を
過ぎた行が対象です。

- `ticket_reminders` の `sent` / `cancelled`（関連する `reminder_deliveries` も連鎖削除）
- 中止済み、またはリマインダーが `sent` / `expired` / `cancelled` の講義

`pending` や `error` の行は削除しないため、未送信のリマインダーが失われることは
ありません。cron等での定期実行を推奨します。

```sh
python -m tools.purge             # RGA_RETENTION_DAYS より古い終了分を削除
python -m tools.purge --days 30   # 保持期間を上書き
python -m tools.purge --dry-run   # 削除せず対象件数だけ表示
```

```sh
# cron例（毎日 04:00）
0 4 * * * cd /opt/rga-assistant && .venv/bin/python -m tools.purge
```

スキーマは `schema_version` テーブルで管理し、起動時にテーブル作成と必要な移行を
自動適用します。`psycopg` の接続先は `postgresql://ユーザー:パスワード@ホスト:ポート/データベース`
の形式で指定します。Windowsでは非同期接続のためにSelectorイベントループを使用します
（`main.py` が起動時に自動設定します）。

### SQLiteからの移行

旧バージョン（SQLite）の状態はPostgreSQLへ自動では引き継がれません。移行せずに起動
すると、監視カテゴリ内のチケットが再登録され、送信済みのチケットへ再度リマインダーが
送られることがあります。Botを停止した状態で、次のコマンドを一度実行してください。

```sh
python -m tools.migrate_sqlite                          # data/rga_assistant.sqlite3 から
python -m tools.migrate_sqlite path/to/legacy.sqlite3   # ファイルを指定する場合
```

`ticket_reminders` / `reminder_deliveries` / `lectures` をコピーします。既存の行は変更
しないため、再実行しても安全です。

## よくあるエラーと対処

| 症状 | 対処 |
|---|---|
| 起動時に `Message Content Intent を有効にしてください。` | Developer Portalで Message Content Intent を有効化し、再起動します。 |
| `RGA_TICKET_CATEGORY_IDS ... must contain valid values.` | カテゴリIDが正の整数のカンマ区切りか確認します。 |
| `RGA_OPERATIONS_CHANNEL_ID is required ...` | `RGA_REMINDER_TARGET` が `operations`/`both` のときは運営チャンネルIDを設定します。 |
| `Set either RGA_OPERATIONS_MENTION_ROLE_ID or ...` | メンションはロールかユーザーのどちらか一方だけを設定します。 |
| リマインダーが送信されない | Botに「チャンネルを見る」「メッセージを送信」「リンクを埋め込む」権限があるか確認します。`error` 状態なら `python -m tools.retry` で復旧します。 |
| 画像やキーワードを送ったのに通知が来る | `RGA_REMINDER_CANCEL_TRIGGER` の設定と運用の合致を確認します。`image` なら、申請者の画像がファイルとして添付されているか（Bot/Webhook投稿やURLの埋め込みは対象外）を確認します。`keyword` なら、`RGA_REMINDER_TRIGGER_TEXT` が投稿本文と完全一致しているか確認します（複数ある場合はカンマ区切り）。 |
| データベースが大きくなってきた | `python -m tools.purge` で保存期間を過ぎた終了分を削除します（`RGA_RETENTION_DAYS`）。 |
| スラッシュコマンドが表示されない | Bot招待時に `applications.commands` スコープを指定したか確認します。グローバル同期の反映には数分かかることがあります。 |
| 講義コマンドで権限エラーになる | `RGA_LECTURE_ROLE_IDS` に対象ロールIDが設定されているか確認します。未設定の場合は管理者のみ実行できます。 |

## テスト

テストはリポジトリ直下で実行します。データベースを使うテストは
`RGA_TEST_DATABASE_URL` に接続先を設定した場合のみ実行され、未設定の場合はスキップ
されます（テスト用データベースを指定してください。テーブルは各テストで初期化されます）。

```sh
python -m unittest discover -s tests

# PostgreSQLを使うテストも含めて実行する場合（Linux/macOS）
RGA_TEST_DATABASE_URL=postgresql://rga:password@localhost:5432/rga_assistant_test \
  python -m unittest discover -s tests
```

```powershell
# PowerShellの場合
$env:RGA_TEST_DATABASE_URL = "postgresql://rga:password@localhost:5432/rga_assistant_test"
python -m unittest discover -s tests
```

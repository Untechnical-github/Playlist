# Playlist（YouTube Music プレイリスト アーティスト順ソート）

YouTube Music のプレイリストを、アーティスト名順に半自動で並び替えるためのツール。
`fetch` → `plan` → `apply` の3段階コマンドで、内容を確認してから反映する。

認証は **OAuthのみ**で完結する（ブラウザの開発者ツールでCookieを取得する作業は不要）。
セットアップはブラウザでのGoogleログインだけなので、スマホからでも行える。

## セットアップ

```
pip install -r requirements.txt
```

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成（または既存のものを選択）
2. 「APIとサービス」→「ライブラリ」から **YouTube Data API v3** を有効化
3. 「APIとサービス」→「認証情報」→「認証情報を作成」→「OAuth クライアント ID」
   - アプリケーションの種類は **「テレビと限定入力デバイス」** を選択
   - 作成後に表示される **クライアントID** と **クライアントシークレット** を控える
4. `auth/oauth_client.json` を作成し、以下の内容で保存する（`.gitignore` 済み、絶対にコミットしない）
   ```json
   { "client_id": "...", "client_secret": "..." }
   ```
5. 以下を実行し、表示されるURLを開いてログイン・許可した後、ターミナルでEnterを押す
   ```
   ytmusicapi oauth --client-id "<控えたクライアントID>" --client-secret "<控えたクライアントシークレット>" --file auth/oauth.json
   ```
   これで `auth/oauth.json`（アクセストークン・リフレッシュトークン、`.gitignore` 済み）が作成される。
   OAuthのリフレッシュトークンはCookieと違い長期間有効で、期限が来ても自動更新されるため、
   基本的に再セットアップは不要。

**プレイリストの公開範囲について**: アーティスト情報の取得には認証なしの `ytmusicapi` を使うため、
対象プレイリストは **「限定公開」または「公開」** である必要がある（「非公開」だと曲情報が
取得できない）。並び替えの反映自体は自分のアカウントのOAuthで行うため、非公開のままでも動く。

## 使い方

```
python cli.py fetch <playlist_id>   # プレイリストを取得し playlist_snapshot.json に保存
python cli.py plan                  # 並び替え案を計算し、コンソール表示 + plan.json に保存
python cli.py apply                 # plan.json の内容を確認プロンプト付きで実際に反映
python cli.py apply --yes           # 確認をスキップ（将来のDiscordコマンド等、非対話実行向け）
```

## 設計方針

- **アーティスト情報の取得元**: 認証なしの `ytmusicapi`（`YTMusic()`、限定公開/公開プレイリストで
  動作）。曲ごとの正確なアーティスト情報が取れる。
- **並び替えの反映**: 公式の **YouTube Data API v3**（`playlistItems.update` で `position` を
  指定する方式）をOAuthで実行する。`ytmusicapi` の非公式な内部API（browseエンドポイント）は
  自作OAuthクライアントからのアクセスが既知の不具合で拒否される
  （[Issue #676](https://github.com/sigma67/ytmusicapi/issues/676),
  [Discussion #682](https://github.com/sigma67/ytmusicapi/discussions/682),
  [Issue #921](https://github.com/sigma67/ytmusicapi/issues/921)）ため使わない。公式APIなら
  同じOAuthトークンで問題なく動作する（実プレイリストで検証済み）。
- **2つの情報源のマージ**: アーティスト情報（`ytmusicapi`）と、並び替えに使う `playlistItem id`
  （公式API）は別々に取得し、**プレイリスト内の位置（何番目か）でマージする**
  （`core/fetch.py`）。videoIdでのマージは使わない。まれにYouTube Music側とYouTube本体側で
  同じ曲でも紐づく動画IDが異なることがあり、実際に24曲中2曲でこのズレが発生することを確認した。
- **「塊」（同じアーティストの曲が2曲以上）だけをアルファベット順にまとめる**: 同じアーティストの
  曲がプレイリスト中に離れて存在していても、`plan` を実行すれば必ず1箇所の塊に統合される
  （`core/planner.py` の `build_plan`）。塊の中の曲順（何がどの位置に来るか）は重視しないため、
  元の相対順のまま。塊同士はアーティスト名のアルファベット/五十音順に並べて前方にまとめる。
- **曲が1曲しかないアーティスト（単独曲）は塊にせず、末尾に元の相対順のまま残す**:
  アルファベット順の中に混ぜてしまうと、単独の新曲を1曲追加するたびに毎回 `plan`/`apply` を
  回さないと違和感が出てしまうため。YouTube Music側で新曲を追加すると通常は末尾に追加される
  ので、単独曲を1曲足しただけなら自動的に末尾の集団に収まり、その曲が2曲目を迎えて
  「塊」になるまでは実行不要になる。
- **状態ファイルは持たない**: 前回実行時の情報を保存せず、常に現在のプレイリストの実際の並び
  だけを見て計算し直す。
- **コラボ曲・不明曲（UGC動画等）の扱い**: これらは単体では「どのアーティストに属するか」を
  機械的に決められないため、**現在どの曲の隣に置かれているか**を見て、隣接している曲と同じ
  アーティスト扱いにする（`core/planner.py` の `_assign_buckets`）。
  - コラボ曲は、掲載アーティストのいずれかが隣接する曲のアーティストと一致すればそちらに所属。
    一致する隣接がなければ、掲載順の先頭アーティストで確定する。
  - アーティスト情報が全く無い曲（UGC動画等）は、隣接するどちらかの曲とそのまま同じ扱いになる。
  - これにより、ユーザーがアプリ上で「コラボ曲やYouTube限定動画をあるアーティストの隣に手動で
    置く」という操作をした場合、それがどの位置にあっても（先頭付近に限らず）そのアーティストの
    曲が合計2曲以上になれば次回の `plan` で塊として統合される。専用の「上書き登録」は不要。
  - どの隣接曲とも同じ扱いにできなかった曲（前後とも不明、あるいは単独）は上記の「単独曲」と
    同様に末尾へ、元の相対順のまま送られる。

## Discord bot

スマホからでも `/sort` コマンドで並び替えを実行できる。`core/` はUIに依存しないので、
CLIと全く同じロジック（`core.fetch` / `core.planner` / `core.apply`）をどちらの方式でも使う。

- **方式A（`bot.py`）**: discord.pyのGateway常時接続方式。実装はシンプルだが、bot専用に
  PCやVPSを常時起動しておく必要がある。
- **方式B（推奨・常時起動不要）**: DiscordのHTTP Interactions方式。**GitHub Actions + Cloudflare
  Workers** で完結し、常時起動するサーバーが不要。以下は方式Bの手順。

## 方式B: GitHub Actions + Cloudflare Workers セットアップ

### 全体の流れ

```
Discord (/sort) → Cloudflare Worker（署名検証・3秒以内に受付応答）
                       → GitHub Actions を起動（repository_dispatch）
                             → github_task.py が core/ を使って fetch/plan/apply
                             → 結果をDiscordのWebhook URLへ直接投稿
```

Cloudflare Workerは「受付」と「オートコンプリート」だけを担当し、実際の処理
（`ytmusicapi`や`requests`を使う重い部分）はすべてGitHub Actions上のPythonが行う。
状態はどこにも保存しない設計（本プロジェクトの一貫した方針）なので、確認ボタンを押した
時点でもう一度最新の状態を取得してから反映する。

**手順は依存関係があるため、必ずこの順番で進めること**（特に「Cloudflare Workerのデプロイ」を
「Discordの Interactions Endpoint URL 設定」より先に行う必要がある。DiscordはURL保存時に即座に
PINGを送って検証するため、Workerが先に動いていないと保存に失敗する）。

### 1. GitHubリポジトリを用意する

1. GitHubで新しいリポジトリを作成する（公開・非公開どちらでも可）
2. このプロジェクトのフォルダを `git init` してそのリポジトリにpushする
3. リポジトリの Settings → Secrets and variables → Actions → New repository secret で
   以下を登録する
   - `OAUTH_TOKEN_JSON`: ローカルの `auth/oauth.json` の中身をそのまま貼り付け
   - `OAUTH_CLIENT_JSON`: ローカルの `auth/oauth_client.json` の中身をそのまま貼り付け

### 2. Discord Developer Portal

1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリを作成
   （既に方式A用に作っている場合はそれを流用してよい）
2. 「General Information」ページで **APPLICATION ID** と **PUBLIC KEY** を控える
3. 「Bot」ページで **Bot Token** を取得（控えておく。他人に共有しない）。Privileged Gateway
   Intentsは不要
4. 「OAuth2」→「URL Generator」で
   - SCOPES: `bot`, `applications.commands`
   - BOT PERMISSIONS: `Send Messages`, `Embed Links`
   - 生成されたURLを開いて、botを自分のサーバーに招待
5. ローカルで以下を実行し、`/sort` コマンドを登録する（1回だけでよい。コマンド定義を変えた時は
   再実行）
   ```
   export DISCORD_APPLICATION_ID="控えたAPPLICATION ID"
   export DISCORD_BOT_TOKEN="控えたBot Token"
   python register_commands.py
   ```
6. 「Interactions Endpoint URL」の設定は**手順4（Cloudflare）が終わってから**行う（後述）

### 3. Cloudflare Workerをデプロイする

1. [Cloudflare](https://dash.cloudflare.com/) にサインアップ/ログイン（無料プランで可）
2. Wrangler CLIをインストールしてログイン
   ```
   npm install -g wrangler
   wrangler login
   ```
3. `worker/wrangler.toml` の `GITHUB_REPO` を自分のリポジトリ（`ユーザー名/リポジトリ名`）に書き換える
4. シークレットを登録する（それぞれ実行するとプロンプトで値の入力を求められる）
   ```
   cd worker
   wrangler secret put DISCORD_PUBLIC_KEY
   wrangler secret put GITHUB_TOKEN
   wrangler secret put GOOGLE_CLIENT_ID
   wrangler secret put GOOGLE_CLIENT_SECRET
   wrangler secret put GOOGLE_REFRESH_TOKEN
   ```
   - `DISCORD_PUBLIC_KEY`: 手順2で控えたPUBLIC KEY
   - `GITHUB_TOKEN`: GitHubの Settings → Developer settings → Personal access tokens で発行した
     トークン（fine-grained の場合は対象リポジトリを選び「Contents: Read and write」権限を付与。
     classicの場合は `repo` スコープ）。`repository_dispatch` の起動に使う
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`: ローカルの `auth/oauth_client.json` の中身と同じ値
   - `GOOGLE_REFRESH_TOKEN`: ローカルの `auth/oauth.json` 内の `refresh_token` の値
     （オートコンプリートでプレイリスト一覧を出すために、Worker自身がGoogleのトークンを
     都度リフレッシュして使う）
5. デプロイする
   ```
   wrangler deploy
   ```
   成功すると `https://playlist-sort-bot.<自分のサブドメイン>.workers.dev` のようなURLが表示される

### 4. Discordの Interactions Endpoint URL を設定する

1. Discord Developer Portalの「General Information」ページに戻る
2. 「Interactions Endpoint URL」に手順3で表示されたWorkerのURLを入力して保存
   （正しく動いていれば保存できる。失敗する場合はWorkerのデプロイやDISCORD_PUBLIC_KEYの値を見直す）

### 5. 動作確認

Botを招待したDiscordサーバーで `/sort` と入力し、プレイリストを選んで実行する。GitHub Actionsの
起動には数秒〜数十秒かかることがある。プレビューが表示されたら「反映する」を押して反映されるか
確認する。GitHub側の実行状況はリポジトリの「Actions」タブから確認できる。

### 旧: 方式A（discord.py、常時起動PC向け）のセットアップ

1. [Discord Developer Portal](https://discord.com/developers/applications) で
   「New Application」からアプリを作成
2. 左メニュー「Bot」→「Reset Token」でトークンを取得（控えておく。他人に共有しない）
3. 「Bot」ページで以下のPrivileged Gateway Intentsは**オンにする必要はない**
   （スラッシュコマンドのみ使用するため）
4. 左メニュー「OAuth2」→「URL Generator」で
   - SCOPES: `bot`, `applications.commands`
   - BOT PERMISSIONS: `Send Messages`, `Embed Links`
   - 生成されたURLを開いて、botを自分のサーバー（または自分だけのテストサーバー）に招待
5. bot を動かすマシン（自分のPC、Raspberry Pi、VPSなど常時起動できる環境）で環境変数を設定して起動
   ```
   export DISCORD_BOT_TOKEN="控えたトークン"
   python bot.py
   ```
   （Windows PowerShellの場合は `$env:DISCORD_BOT_TOKEN = "..."`）
6. サーバー上で `/sort` と入力すると、自分が所有するプレイリストがオートコンプリートで
   候補表示される（公式APIの `playlists.list(mine=true)` を利用）。選んで実行すると、
   並び替え案がプレビュー表示され、「反映する」ボタンを押すと実際に反映される。
   本人以外はボタンを押せない。

### 補足

- bot 自体は `auth/oauth.json` / `auth/oauth_client.json` をそのまま使う。OAuthのリフレッシュ
  トークンは長期間有効で自動更新されるため、Cookie方式と違って定期的な手動再取得は基本的に不要。
- botを動かすマシンを長時間止めずに起動し続ける必要がある（GitHub Actions等のスケジュール実行では
  スラッシュコマンドの常時待受けはできない）。無料枠のあるクラウド（Railway、Fly.io等）や、
  常時起動しているPC/Raspberry Piでの運用を想定。
- （方式A）`/sort` 実行のたびに毎回 `fetch` → `plan` を計算し直す。ボタンを押した時点の
  プレイリストの状態が最初の確認時と変わっていた場合は安全のため中止し、再実行を促す。
- （方式B）状態を一切引き継がない設計上、「反映する」を押した時点でもう一度最新の状態を取得して
  計算し直してから反映する（プレビュー表示時との差分チェックはしない。押した時点で見えている
  内容が最新のプレイリストと異なっていた場合、押した時点の最新状態を基準に反映される）。

## 既知の限界・今後の検証事項

- **五十音順の限界**: API が読み仮名を提供しないため、漢字アーティスト名の厳密な五十音順は
  出せない。現状は Unicode 正規化 (`NFKC` + casefold) した文字列のコードポイント順。
- **非公開プレイリストは非対応**: アーティスト情報の取得が認証なしアクセスに依存しているため、
  「非公開」設定のプレイリストは扱えない（「限定公開」であれば問題ない）。
- **Discord bot はローカルでの動作確認まで**: 方式A（`bot.py`）・方式B
  （`worker/src/index.js`, `github_task.py`, `register_commands.py`）ともに
  import・コマンド登録・構文・ペイロード組み立てまでは確認済みだが、実際のDiscordサーバー上
  でのやり取り（Cloudflare Workerへの署名付きリクエスト到達、GitHub Actionsの起動、Discordへの
  Webhook投稿）は未検証。初回はテスト用サーバーで一通り動作確認すること。
- **方式Bの起動遅延**: GitHub Actionsのジョブ起動には数秒〜数十秒かかることがある。Discordの
  フォローアップメッセージ送信は該当インタラクションのトークン発行から15分以内に行う必要が
  あるが、通常の処理時間なら十分間に合う。
- **方式BのGitHubトークン権限**: `repository_dispatch` の起動には対象リポジトリへの書き込み
  権限相当が必要。fine-grained PATを使う場合は「Contents: Read and write」権限が必要になる点に
  注意。

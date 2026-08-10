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
- **「塊」（同じアーティストの曲が2曲以上）だけをアルファベット順にまとめる**: 本当に2曲以上ある
  アーティストは、プレイリスト中に離れて存在していても `plan` を実行すれば必ず1箇所の塊に
  統合される（`core/planner.py` の `group_tracks` / `_assign_buckets`）。位置に関係なく常に
  そのアーティスト名で確定するため、途中に何を挟んでも分断されない。塊の中の曲順（何がどの位置に
  来るか）は重視しないため、元の相対順のまま。塊同士はアーティスト名のアルファベット/五十音順に
  並べて前方にまとめる。
- **位置（隣に何があるか）は一切見ない**: 以前は「隣接する塊に取り込む」という位置ベースの
  推測もしていたが、無関係な曲まで巻き込む誤爆が多かったため撤廃した。今はアーティストの
  一致（表記ゆれ・カタカナ/ローマ字変換・`artist_groups.json` 登録を含む）だけで判定する、
  位置に依存しないシンプルな仕組みになっている（`core/planner.py` の `_assign_buckets`）。
  - コラボ曲は、掲載されているアーティストのいずれかが本当に2曲以上あればそちらに所属する。
    該当が無ければ掲載順の先頭アーティストで確定する（それも単独曲としての扱いになる）。
  - アーティスト情報が全く無い曲（UGC動画等）は、判定材料が何もないため常に「不明」として
    末尾へ、元の相対順のまま送られる。
  - 曲が1曲しかないアーティスト（単独曲）は、隣に何があっても取り込まれることなく、常に
    末尾へ、元の相対順のまま送られる。
- **状態ファイルは持たない**: 前回実行時の情報を保存せず、常に現在のプレイリストの実際の並び
  だけを見て計算し直す。
- **アーティストグループ（`artist_groups.json`）**: 本来は別アーティスト表記だが、同じ企画・
  同一キャラクター関連などの理由でまとめて扱いたい場合、`artist_groups.json` にプレイリストID
  単位で登録できる（`core/aliases.py` の `load_artist_groups`）。位置に頼らず明示的に指定する
  ため、アルファベット順が離れているアーティスト同士でも確実に1つの塊にまとまる。
  ```json
  {
    "プレイリストID": {
      "表示したいグループ名": ["実際のアーティスト表記1", "実際のアーティスト表記2"]
    }
  }
  ```
  このファイルに秘密情報は含まれないため、`.gitignore` の対象にはしておらず、GitHub Actions
  実行時もリポジトリの内容としてそのまま読み込まれる。
  - コラボ曲についても、掲載アーティストのいずれかが既にグループ内で本当に複数曲ある
    （コラボ曲での登場も含む）なら、位置に関係なく即座にそのグループに所属する。
- **表記ゆれの自動統合**: "Macaroni Empitsu" と "macaroni enpitsu" のような、大文字小文字や
  1文字程度の違いしかない表記は、`artist_groups.json` への登録なしで自動的に同一アーティストと
  みなして統合する（`core/planner.py` の `_auto_merge_similar_names`）。誤爆を避けるため、
  ある程度長い名前同士（6文字以上）で類似度が高い場合のみ対象にしており、"Aimyon"/"Aimer" の
  ような短く紛れやすい名前同士は統合されない。これは文字列そのものの近さで判定するため、
  一度も並び替えていない状態でも、何度実行しても常に同じ結果になる（位置には依存しない）。
- **かな表記とローマ字表記の自動統合**: "ヨルシカ"/"Yorushika"、"なとり"/"natori" のような、
  ひらがな・カタカナ表記とそのローマ字表記は`pykakasi`でローマ字に変換して比較し、登録なしで
  自動的に同一アーティストとみなして統合する（`core/planner.py` の `_auto_merge_transliterations`）。
  漢字を含む表記（人名の読みは辞書変換だけでは一意に決まらないため）は対象外。
- **プレビューは何も省略しない**（`github_task.py` の `build_embeds` / `chunk_embeds`）:
  Discordの制約（embed 1個あたりフィールド25個・6000文字、1メッセージあたりembed最大10個）を
  超える場合は、省略するのではなく複数のembed・複数のメッセージに分割して全件表示する。
  1つの塊の曲名一覧だけで1024文字を超える場合も、「（続き）」フィールドに分割して全曲表示する。

## Discord bot

スマホからでも `/sort` コマンドで並び替えを実行できる。`core/` はUIに依存しないので、
CLIと全く同じロジック（`core.fetch` / `core.planner` / `core.apply`）を使う。

DiscordのHTTP Interactions方式を採用しており、**GitHub Actions + Cloudflare Workers** で
完結する。常時起動するサーバーは不要。

## セットアップ手順

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
   成功すると `https://playlist.<自分のサブドメイン>.workers.dev` のようなURLが表示される
   （Worker名は `wrangler.toml` の `name` で変更できるが、小文字英数字とハイフンのみ使用可）

### 4. Discordの Interactions Endpoint URL を設定する

1. Discord Developer Portalの「General Information」ページに戻る
2. 「Interactions Endpoint URL」に手順3で表示されたWorkerのURLを入力して保存
   （正しく動いていれば保存できる。失敗する場合はWorkerのデプロイやDISCORD_PUBLIC_KEYの値を見直す）

### 5. 動作確認

Botを招待したDiscordサーバーで `/sort` と入力し、プレイリストを選んで実行する。GitHub Actionsの
起動には数秒〜数十秒かかることがある。プレビューが表示されたら「反映する」を押して反映されるか
確認する。GitHub側の実行状況はリポジトリの「Actions」タブから確認できる。

### 補足

- Worker/GitHub Actionsは `auth/oauth.json` / `auth/oauth_client.json` の中身をそのまま
  Secretsとして使う。OAuthのリフレッシュトークンは長期間有効で自動更新されるため、Cookie方式と
  違って定期的な手動再取得は基本的に不要。
- 状態を一切引き継がない設計上、「反映する」を押した時点でもう一度最新の状態を取得して
  計算し直してから反映する（プレビュー表示時との差分チェックはしない。押した時点で見えている
  内容が最新のプレイリストと異なっていた場合、押した時点の最新状態を基準に反映される）。
- GitHub Actionsのジョブ起動には数秒〜数十秒かかることがある。Discordのフォローアップメッセージ
  送信は該当インタラクションのトークン発行から15分以内に行う必要があるが、通常の処理時間なら
  十分間に合う。
- `repository_dispatch` の起動にはGitHubトークンに対象リポジトリへの書き込み権限相当が必要。
  fine-grained PATを使う場合は「Contents: Read and write」権限が必要になる点に注意。

## 既知の限界

- **五十音順の限界**: API が読み仮名を提供しないため、漢字アーティスト名の厳密な五十音順は
  出せない。現状は Unicode 正規化 (`NFKC` + casefold) した文字列のコードポイント順。
- **非公開プレイリストは非対応**: アーティスト情報の取得が認証なしアクセスに依存しているため、
  「非公開」設定のプレイリストは扱えない（「限定公開」であれば問題ない）。
- **プレイリストの並び順設定**: プレイリストの並び順が「カスタム順」以外（例えば「追加順」等）に
  なっていると、公式APIが `position` 指定を拒否する（`manualSortRequired`）。その場合はエラー
  メッセージがDiscordに表示されるので、YouTube Musicアプリで並び順を「カスタム順」に戻せば良い。

## 曲のスコアリング（`score_playlist.py`）

並び替えツールとは独立した別のスクリプト。プレイリストの曲を、YouTube再生回数を基にスコアリングし、
人気順にソートしたCSV/JSONを出力する。

当初はSpotifyのpopularityスコアも組み合わせる予定だったが、2026年2月のSpotify Web APIの仕様変更
（Development Modeのアプリは`popularity`フィールドを取得できなくなり、それを回避する
「Extended Quota」申請にはPremiumアカウントが必須になった）により、Premiumアカウントを使いたくない
場合はSpotify側のデータが実質取得できないため、YouTube再生回数のみでの判定にしている。

### セットアップ

```
pip install -r requirements.txt
cp .env.example .env
```

`.env` を開いて以下を埋める。

- `YOUTUBE_API_KEY`: Google Cloud Consoleで有効化した **YouTube Data API v3** のAPIキー
  （認証情報 → APIキーを作成。OAuthは不要、キーだけでよい）
- `YTMUSIC_AUTH_FILE`: 省略可。ytmusicapiの`browser`認証ファイルのパス（デフォルト
  `auth/headers_auth.json`）。無ければ認証なし（限定公開/公開プレイリストのみ）で取得を試みる

### 使い方

```
python score_playlist.py <プレイリストID>
python score_playlist.py <プレイリストID> --output scores.json
```

### 設計メモ

- **自分のプレイリストの曲は`search.list`を使わない**: `fetch_playlist_tracks`が
  ytmusicapi経由で取得するtrackには、YouTube Music側で既に紐付いている`videoId`が含まれている。
  対象プレイリストの曲はすべて自分のプレイリストに入っている＝ytmusicapiがvideoIdを返せる曲な
  ので、初回取得でも`search.list`（100クォータ・呼び出し間隔の制限あり）による検索を行わず、
  判明済みのvideoIdで`videos.list`（1クォータ・制限なし）だけ叩けばよい（`get_youtube_view_count`
  の`known_video_id`引数）。`search.list`が必要になるのは、コラボ・カバー候補の探索
  （曲名だけで検索して自分のプレイリストに無い動画を探す、後述）だけになる。
- **同じ曲を何度も検索しない**: 曲名・アーティスト名の組み合わせごとにキャッシュし、同じ曲を
  複数回検索しない。
- **見つからなかった曲はスコア0**: YouTubeでヒットしなかった曲は`view_count`が0になり、ログに
  「no match」として出力される。出力結果でも`video_id`が空欄になる。
- **再生回数の正規化はlogスケール**: 再生回数は桁違いに差が出る（例:
  1,000回とその1,000倍の1,000,000回のように、素の値でmin-max正規化すると最上位の1曲以外が
  ほぼ0点になってしまう）ため、`log(views + 1)`を取ってからmin-max正規化する
  （`scoring.py` の `normalize_view_counts`）。
- **リトライ**: API呼び出しに、失敗のたびに待ち時間を伸ばす簡易リトライ（最大5回）を入れている。
  それでも失敗した場合はその曲を「見つからなかった」扱いにする。
- **`search.list`のペース制限**: YouTube Data API v3は「1分あたりの検索回数」が厳しく、
  何百曲もの`search.list`を間隔を空けずに連続で呼ぶと`429 rateLimitExceeded`
  （"Search Queries per minute"）が続けて発生し、実行がほぼ進まなくなることがある。これを防ぐため
  `MIN_SEARCH_INTERVAL_SECONDS`（既定1.5秒、`YOUTUBE_SEARCH_MIN_INTERVAL`環境変数で調整可）だけ
  呼び出し間隔を空けてから検索する。それでも429になった場合はリトライの待ち時間を通常より長く
  （既定30秒、`RATE_LIMIT_BACKOFF_SECONDS`）取る。
- **検索結果のディスクキャッシュ（`youtube_view_cache.json`）**: 曲名・アーティスト名の組み合わせ
  ごとに`video_id`・再生回数を実行間で永続化する。期限は設けておらず、一度調べた曲は
  基本的にずっとキャッシュから再利用する（再生回数は基本的に増加のみで減らないため、
  古い値でも判定を誤らせにくいという前提）。gitignore対象。
  Discordの`/score`（GitHub Actions経由）でも`actions/cache`でこのファイルをジョブ間で保持している。
  `/score`コマンドでは、しきい値の80%以上・未満のいわゆる境界線付近の曲だけキャッシュ済みでも
  再取得する（`BORDERLINE_THRESHOLD_RATIO`、詳細は後述）。このとき`video_id`は既にキャッシュで
  判明しているため`search.list`（100クォータ・呼び出し間隔の制限あり）はやり直さず、
  `videos.list`（1クォータ）だけで統計情報を更新する。再取得した値が既存のキャッシュ値より
  小さい場合（動画差し替え等）は更新せず、大きい方の値を維持する。
- **YouTube Data API v3のクォータに注意**: `search.list`は1回100ユニット消費し、デフォルトの
  1日あたり10,000ユニット枠では**1日あたり約100回分**が上限の目安になる。対象プレイリストの
  曲自体の初回取得は前述の通り`search.list`を使わないため、この制限にはほぼ引っかからない。
  実際に`search.list`を消費するのはコラボ・カバー候補の探索（`/score`実行ごとに最大90曲分、
  `COVER_DISCOVERY_MAX_TRACKS_PER_RUN`）だけで、通常のスコアリングと取り合う心配が無いため
  上限を100クォータの目安近くまで引き上げている。しきい値未満の曲が数百曲規模でも、
  1日1回`/score`を実行し続ければ数日〜1週間程度で全曲の探索が完了する計算になる。
  1日あたりのクォータを使い切った場合（`429`・メッセージに「per day」を含む）は、待っても
  当日中は回復しないためリトライせず即座に諦める（`_is_daily_quota_exceeded`）。この場合
  未取得の曲は「見つからなかった」としてキャッシュに固定されることはなく、再生回数0の
  未取得扱いのままその回の判定に使われ、次回実行時に改めて取得される。

### Discordの`/score`コマンド

`/sort`と同じ仕組み（Cloudflare Worker → GitHub Actions）で、Discordから実行できる。
CLI版（`python score_playlist.py`）とは動作が異なり、**自分の全プレイリストを自動集計対象**にし、
しきい値以上の曲を「Playlist」という名前のプレイリストの内容と**完全に同期**させる
（`github_score_task.py`）。

```
/score threshold:<しきい値（万回再生単位）>
```

- 対象プレイリストの指定は不要。自分が所有するプレイリストのうち、**タイトルが「Playlist」で
  始まるもの**（集計先の「Playlist」自身や「Playlist II」等）、**「English Songs」**、
  **「高く評価した音楽」**（YouTube Musicが自動生成する高評価動画のプレイリスト。他の
  集計対象プレイリストと曲が重複しやすく、重複追加の原因になるため除外）を除いた
  すべてが集計対象になる（`get_source_playlists`、`EXCLUDED_EXACT_TITLES`）
- `threshold`は「万回再生」単位。例: `100` → 100万回再生以上、`5000` → 5000万回再生以上、
  `20000` → 2億回再生以上の曲を対象にする
- **「Playlist」は毎回、今回のしきい値を満たす曲の集合と同期する**:
  - まだ入っていない・条件を満たす曲は追加する
  - 既に入っていて、かつ今回**再生回数を確認できた**上でしきい値を満たさなくなった曲
    （前回より厳しい条件を選んだ場合等）は自動的に削除される
  - 既に入っているが、集計対象のどのプレイリストにも出てこない曲、または再生回数の取得に
    失敗した（クォータ超過等）曲は、**しきい値未満と確定できないため削除せず保留する**
    （メッセージに「保留」として表示される）。手動で無関係な曲を「Playlist」に入れている場合も、
    以前のバージョンとは異なり自動削除はされなくなった
  - 追加・削除・保留はいずれも今回実行分だけがメッセージに表示される（何も省略せず、長い場合は
    複数メッセージに分割される）
  - 同じ曲が複数の集計対象プレイリストに入っていても、追加は1回だけ行われる（同一video_idを
    今回既に追加済みなら重複して追加しない。でないとYouTube側が「たった今追加した重複」として
    `409 Conflict`を返しジョブが失敗する）
  - **実行がどこまで進んだかを毎回メッセージに含める**: 「対象X曲中Y曲を確認済み」という形で、
    対象曲のうち実際に再生回数を確認できた曲数を表示する（`verified_count`）。追加・削除件数だけ
    見ても、クォータ切れで一部の新規曲が未確認のまま今回の集計から漏れているのか、単に全曲確認
    した上でしきい値を満たさなかっただけなのかは区別できないため。1日あたりのYouTube検索
    クォータを使い切った場合は「⚠️ 本日のYouTube検索クォータを使い切ったため、一部の曲は
    確認できていません。翌日以降に`/score`を再実行すると続きから処理されます。」という警告も
    追加され、再実行すべきかどうかが分かるようになっている（`was_daily_quota_exceeded`）
- プレイリストの取得・追加・削除にはOAuth（並び替えツールと同じ`auth/oauth.json`）を使う。
  加えて、GitHub Actionsのリポジトリシークレットに`YOUTUBE_API_KEY`（YouTube Data API v3の
  APIキー）と`DISCORD_BOT_TOKEN`（コラボ・カバー判定ボタンのエラー通知用、後述）の登録が必要
- **境界線付近の曲だけ再生回数を再取得する**: キャッシュに値がある曲でも、しきい値の80%以上
  100%未満（`BORDERLINE_THRESHOLD_RATIO`）の場合は、その場でYouTubeから再取得し直した
  最新の値で判定する。しきい値を大きく超えている曲は再生回数が減ることがなく今後も条件を
  満たし続け、大きく下回っている曲も次回実行までに逆転する可能性は低いため、時間・クォータの
  節約のためにこの範囲だけ再取得する。確定済みのコラボ・カバー候補（後述）についても、
  曲自体との最大値が境界線付近であれば同様に再取得する。
- **コラボ・カバー動画の自動探索とDiscordでの確認（`cover_candidates.json`）**: 曲自体の
  再生回数だけではカバー元・コラボ相手の人気度を反映できないケースがあるが、これを信頼できる
  形で自動判定するのは難しいため、**自動探索＋人間による最終確認**という形にしている
  （以前あった`score_overrides.json`による手動一覧管理は廃止）。
  - `/score`実行時、既存のスコアリングより先に、**しきい値未満と分かっている曲**（＝前回までに
    自身の再生回数がキャッシュ済みの曲）についてだけ、曲名のみで検索し、本人名義の動画とは
    別の候補（カバー・コラボ動画等）を探す（`discover_cover_candidates`）。既にしきい値を
    超えている曲は候補を足しても結果が変わらないため対象外
  - 曲名だけの検索は無関係な動画（別の曲・反応動画・まとめ動画等）まで拾ってしまいやすいため、
    **動画タイトルに曲名と元のアーティスト名の両方が含まれているものだけ**を候補にする
    （例:「曲名 (Cover) - 元のアーティスト名」）
  - 加えて、別人によるカバー・コラボではなく**本人による別アップロード**（MV・リリックビデオ・
    Audio版等）を除外するため、候補のチャンネル名/アーティスト名がクレジット上のアーティスト名と
    実質同じもの（`_looks_like_same_artist`）も候補から除外する（動画タイトルにアーティスト名が
    入っていても、投稿者が本人自身なら除外する）
  - **検索は2段階**: まずクォータを消費しない`ytmusicapi`の検索（"songs"・"videos"の両方。
    カバー曲がYouTube Musicの正式カタログに乗っていることもあるため）を試し、そこで
    見つからなかった曲だけYouTube Data APIの`search.list`（クォータ消費）でフォールバック
    検索する。曲ごとにどちらを試したかは`cover_candidates.json`内の`_meta`に記録され、
    両方試して見つからなかった曲を毎回無駄に検索し直すことはない
  - 見つかった候補はDiscordに「はい/いいえ」ボタン付きで通知される。複数候補があるときは
    1メッセージにまとめて表示される（最大`COVER_CANDIDATES_PER_MESSAGE`件ずつ）
  - **Discord APIのレート制限に対応**: 1回の実行で見つかる候補が多いと、通知メッセージを
    連続で送ることになりDiscord側の429（レート制限）を受けることがある。この場合は
    レスポンスの`retry_after`秒だけ待って自動的に再試行する（`_post_discord_with_retry`、
    最大`DISCORD_RATE_LIMIT_MAX_RETRIES`回）。この仕組みは通常の追加/削除メッセージ・
    エラー通知にも共通で使われる
  - ボタンを押すと判定（`cover_candidates.json`）が確定し、以降そのしきい値判定に使われる。
    「はい」と判定された候補の再生回数は、曲自体の再生回数と合わせた最大値がしきい値判定に使われる
    （Playlistに追加されるのはあくまで曲自体＝クレジット通りの動画）
  - 一度提示した候補（はい/いいえ問わず）は二度と提示しない。判定結果はGitHub Actionsから
    リポジトリへ直接コミットして永続化される（`core/repo_commit.py`）。コミットは1曲ごとではなく
    `COVER_DISCOVERY_COMMIT_BATCH_SIZE`件（既定20件）ごとにまとめて行う
  - 1回の`/score`実行で処理する曲数は`COVER_DISCOVERY_MAX_TRACKS_PER_RUN`（既定2000曲）まで。
    `ytmusicapi`はGoogle側のクォータを消費しないためこの上限はクォータではなく処理時間・
    非公式APIへの配慮で決めているだけで、通常規模のプレイリストなら1回の実行で全曲処理できる。
    YouTube検索へのフォールバックだけは`COVER_DISCOVERY_YOUTUBE_SEARCH_MAX_PER_RUN`
    （既定90回）でクォータを制限しており、ytmusicapiで見つからなかった残りの曲だけ
    日を跨いで補完的に探索される（見つけられなくても再生回数記録自体は初回から全曲終わる）
    （通常のスコアリングは`known_video_id`のおかげで`search.list`をほぼ使わないため、
    1日あたりのクォータ約100回分の大半をこちらに割り当てられる）
  - **ボタン確定時のエラー通知**: 「はい/いいえ」ボタンを押した後の判定記録処理
    （`cover_decide`）は、元の`/score`実行のinteraction_tokenを持たない（Workerが
    ボタン操作のinteractionには既に同期応答を返しているため）。そのため記録に失敗した場合は
    Botトークン（`DISCORD_BOT_TOKEN`）でボタンが押されたチャンネルに直接エラーメッセージを
    送る（`post_channel_message`）。この用途のためだけに、GitHub Actionsのリポジトリ
    シークレットに`DISCORD_BOT_TOKEN`（[Discord Developer Portal](https://discord.com/developers/applications)の
    「Bot」ページで取得したもの。手順2で控えたものと同じ）を登録しておく必要がある

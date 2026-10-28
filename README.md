# ytm-import

Spotify の「お気に入りの曲」を、YouTube Music の「高評価した音楽」に移すツール。Spotify の曲一覧は [Exportify](https://exportify.app/) で書き出した CSV から読み込む。
Claude Code から頼んで進めるか、ターミナル上の画面（TUI）で自分で操作する。YouTube Music のアーティストをまとめてフォローすることもできる。

- 確実に一致した曲だけを自動で決め、曖昧な曲は保留して後からまとめて確認する
- アニソン・声優・舞台作品の曲で起きやすい誤一致（同名曲の歌唱者違い、`ver.` 違い、ライブ版、TV サイズ、Off Vocal など）を避ける
- 照合（YouTube Music で曲を探す処理）の間は、YouTube Music のアカウントに一切アクセスしない。高評価は、対象の曲を確認したうえで、最後に実行を指示したときだけ付く
- どこで中断しても再開でき、何度実行しても同じ曲に重複して高評価を付けない

## 必要なもの

- macOS / Linux / Windows（macOS で動作確認済み）
- [uv](https://docs.astral.sh/uv/)（[インストール方法](https://docs.astral.sh/uv/getting-started/installation/)。macOS なら `brew install uv`。このリポジトリでは `mise install` でも入る。シェルで mise を有効にしていない場合は、以下のコマンドの `uv` を `mise exec -- uv` に読み替える）
- Exportify で書き出したお気に入りの曲の CSV（日本語・英語どちらのヘッダーにも対応）

## 使い方

```sh
uv sync
```

### 推奨：Claude Code から使う

このリポジトリには Claude Code の Skill（`.claude/skills/ytm-import/SKILL.md`）が入っている。リポジトリで Claude Code を開き、次のように頼む。

> 移行を進めて（CSV は ~/Downloads/liked.csv）

Claude が次の作業を進める。ユーザーが行うのは、判断の承認と認証の設定だけになる。

1. CSV の取り込みと照合（バックグラウンドで実行）
2. 自動一致の点検（誤一致の可能性があるものを抽出し、要約して報告）
3. MV 版の一括採用（件数と例を見せて承認を取る）
4. 要確認の曲の判断の下書き（20 曲ずつ表で提案し、承認した分だけ記録する）
5. 認証の案内（Cookie はユーザー自身が `! pbpaste | mise exec -- uv run ytm-import auth --authuser 0` で設定する）
6. 高評価の適用：dry-run の結果とアカウント名を示し、**明示的に承認したときだけ**実行する
7. （任意）アーティストのフォロー：名前の一覧からアーティストページを特定し、承認した分だけフォローする

YouTube Music の検索で見つからない曲は、Claude が Web 検索で公式音源（`Provided to YouTube by …` の Topic 動画）を探し、`add-video` で候補に加えてから判断を提案する。

Skill の安全規則：

- アカウントへの書き込みは承認後の `apply --execute` と `follow --execute` だけ
- 認証ファイルは読まない・表示しない
- DB は CLI を通してのみ変更する

### TUI で手動操作する

曲を試聴しながら自分で選びたいときや、Claude Code を使わないときは TUI を使う。Claude Code と並行して使ってもよいが、照合を同時に 2 つ動かさないこと。

```sh
# CSV を取り込んで TUI を起動（2 回目以降は `uv run ytm-import` だけでよい）
uv run ytm-import --csv ~/Downloads/liked.csv
```

TUI の流れ:

| 画面 | キー | 内容 |
| --- | --- | --- |
| ホーム | `m` | **照合**：全曲を YouTube Music で検索し、確実な曲だけ自動で決める。`s` で停止。止めても結果は保存され、`m` で続きから再開する |
| ホーム | `r` | **確認**：要確認・見つからない・後で判断の曲を順に処理する |
| ホーム | `l` | **一覧**：自動一致を含む全曲の結果。`f` で状態ごとに絞り込み、`/` で文字検索、Enter で開いて修正 |
| ホーム | `v` | **MV 版の一括採用**：YouTube Music に音源が無く公式 MV だけが一致する要確認の曲を、一覧で確認してからまとめて確定する（`p` で MV を開いて確認できる） |
| ホーム | `o` | **設定**：Premium 限定の曲の扱い（追加する／確認する／追加しない） |
| ホーム | `a` | **適用**：高評価を付ける曲の一覧を確認し、`x` → アカウント名を確認 → `yes` と入力すると実行する |

確認画面のキー:

| キー | 動作 |
| --- | --- |
| `↑` `↓` / Enter / `1`〜`9` | 候補を選んで確定し、次の曲へ進む |
| `s` | スキップ（移行しない） |
| `d` | 後で判断（キューの最後に回す） |
| `/` | 検索語を編集して再検索（結果は候補に追加される） |
| `p` / `o` | 候補を YouTube Music で開く / 元の曲を Spotify で開く（聴き比べ用） |
| `u` | 未決定に戻す |
| `←` `→` | 前の曲 / 次の曲 |

候補の「判定」列で `✓ 一致` と表示されるのは、すべての厳密チェックを通った候補。点数は並び順の目安で、メモ列に一致しなかった理由（例: `候補のみ: TVサイズ/ショート`、`歌唱者の構成が違う`、`長さが +12 秒違う`）を表示する。

### YouTube Music の認証（適用の前に一度だけ）

Cookie が途中で無効にならないよう、**シークレットウィンドウ**で取得するのがよい（[ytmusicapi のブラウザ認証](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html)）。

1. シークレットウィンドウで music.youtube.com にログインし、開発者ツール（macOS: ⌥⌘I）の Network タブで `browse` のリクエストを選ぶ
2. Headers タブの Request Headers で `x-goog-authuser` の値（`0`、`1` など。複数アカウントにログインしているときに使うアカウントの番号）を確認する
3. `cookie` の値を右クリック → Copy value でコピーする
4. **ログアウトせずにシークレットウィンドウを閉じる**（ブラウザ側で Cookie が更新されると、コピーした Cookie はすぐ無効になるため）
5. 貼り付けて保存する

```sh
pbpaste | uv run ytm-import auth --authuser 0   # macOS。他の OS は `uv run ytm-import auth --authuser 0` を実行して貼り付け、Ctrl-D
```

`ログイン確認 OK: <アカウント名>` と表示されれば完了。違うアカウント名が出たら `--authuser` の番号を変える。

貼り付けるのは `cookie` の値だけでもよいし、リクエストヘッダー全体（Chrome の Copy request headers、Raw 表示、Firefox / Safari の形式のどれでも）でもよい。リクエスト行、HTTP/2 の疑似ヘッダー（`:authority` など）、`content-encoding` など、そのまま送ると失敗するヘッダーは保存前に取り除く。

- 認証情報は `~/.config/ytm-import/browser.json` に保存する（リポジトリの外）。画面やログには表示しない
- macOS / Linux では権限 600 で保存し、権限が緩いファイルは読み込まない（Windows には同等の権限ビットが無いため、このチェックは行わない）
- 実行中に Google が Cookie の一部（`__Secure-*PSIDTS`）を更新したら、その値を送信に使い、認証ファイルにも書き戻す（ログには Cookie の名前だけを記録する）
- それでも `401 Unauthorized` やログイン確認の失敗が出たら、Cookie が無効になっている。手順をやり直す（付与済みの曲は記録されているので、再実行すれば続きから再開する）
- ヘッダーにはログイン Cookie が含まれる。移行が終わったら、このファイルを削除し、music.youtube.com からログアウトすると無効化できる

### その他のコマンド

```sh
uv run ytm-import status                 # 現在の状態（JSON）
uv run ytm-import match [--retry-open]   # 照合だけ実行（Ctrl-C で中断、再実行で続きから）
uv run ytm-import redecide               # 保存済みの検索結果で判定だけやり直す（通信なし。手動の判断は保持）
uv run ytm-import audit                  # 誤一致の可能性がある自動一致と、惜しい未決定の曲
uv run ytm-import review-list            # 未決定の曲と候補（1 行 1 曲の JSON）
uv run ytm-import search <uri> "<検索語>"  # 再検索して候補に追加
uv run ytm-import add-video <uri> <videoId>...  # Web 検索などで見つけた動画を候補に追加（曲情報を取得するだけ）
uv run ytm-import decide <uri> --pick <videoId> | --pick-n N | --skip | --defer | --reset
uv run ytm-import decide --file decisions.json   # まとめて記録（全件を検証してから記録する）
uv run ytm-import accept-mv [--execute]  # MV 版の一括採用（既定は dry-run）
uv run ytm-import config premium [include|review|exclude]  # Premium 限定の曲の扱い（既定 include）
uv run ytm-import whoami                 # 認証情報のアカウント名（読み取りのみ）
uv run ytm-import apply [--execute]      # 高評価の適用（既定は dry-run。--execute でアカウントに書き込む）
uv run ytm-import follow artists.json [--execute]  # アーティストのフォロー（既定は dry-run）
uv run ytm-import export out.csv         # 照合結果を CSV に書き出す（表計算ソフトでの確認用）
```

状態は `data/state.sqlite` に保存する（`--data` で変更可）。最初からやり直したいときはこのファイルを削除する。

Spotify で曲が増えたら、書き出し直した CSV を `import` する（TUI なら `--csv`）。追加された曲だけが未照合として加わり、それまでの判断と付与の記録はそのまま残る。

`follow` に渡す JSON は `[{"name": "Spotify での名前", "channel_id": "UC…"}]` の配列。`channel_id` は YouTube Music のアーティストページ（`music.youtube.com/channel/UC…`）の ID。すでにフォローしているアーティストは飛ばす。YouTube Music でアーティストをフォローすると、YouTube でもそのチャンネルを登録したことになる。

詳細なログは `data/ytm-import.log` に追記される。照合の判定（選んだ候補・理由・検索クエリ）、確認画面での操作、再検索、高評価の付与・失敗を 1 行ずつ記録する。認証情報は記録しない。

```sh
grep "match review" data/ytm-import.log      # 要確認になった曲と理由
grep "like " data/ytm-import.log             # アカウントへの書き込み履歴
```

## 照合の仕組み

1 曲ごとに次の順で検索し、自動で決められた時点で打ち切る（1 曲あたり平均 2 リクエスト程度。2,700 曲で約 40 分）。

1. ISRC で検索（YouTube Music は ISRC を返さないため、検索結果の 1 位を「ISRC 検索 1 位」として参考にする）
2. `曲名 アーティスト` で検索（日本語ロケール）
3. `基本の曲名 メインアーティスト`、アルバム名に `ver.` 表記があればそれを加えた検索
4. 英語ロケールで ISRC と `曲名 アーティスト`（Spotify 側が英語表記のアーティスト向け。同じ videoId の候補は日英の表記をまとめて比較する）

候補は、次の表の条件を**すべて**満たすときだけ「一致」とみなす。

| 項目 | 条件 |
| --- | --- |
| 曲名 | 括弧や ` - ` 以降の副題、`feat.` を除いた基本の曲名がほぼ同じ |
| 版 | ライブ／リミックス／インスト・Off Vocal・カラオケ／TV サイズ・ショート／アコースティック／カバー／ソロ／言語違い／`○○ ver.` などの表記が一致する。`ver.` の中身は厳密に比べる（`Ale ver.` と `Sal ver.`、`純那&なな ver.` と `双葉&香子 ver.` は別物）。曲名に表記が無くアルバム名だけに書かれている場合（キャラソンのシングルに多い）も考慮する |
| アーティスト | Spotify 側と候補側のクレジットが十分に重なる。`キャラ名(CV.声優名)` はキャラ名と声優名の両方で比べる。一方、`ユニット名 (A)` と `ユニット名 (B)` のような括弧違いや、`μ's` と `メンバー3人 + μ's` のような構成違いは別の歌唱とみなす |
| 長さ | 差が 3 秒以内（ISRC 検索 1 位は 6 秒以内） |
| 種類 | 公式音源である（MV やユーザー投稿動画ではない） |

音源が見つからない曲は、Spotify のアルバム名で YouTube Music のアルバムページも調べ、再生できる収録曲を候補に加える。一部のレーベルは、アルバムの収録曲が再生不可か、MV でしか配信されていないためである。MV は自動では決めず、`v` の一括採用か確認画面で選ぶ。一括採用の対象は、曲名・版・アーティスト・アルバム・長さ（3 秒以内）がすべて一致する MV だけである。

YouTube Music Premium 会員だけが再生できる曲は、ログインしていない検索には出てこず、アルバムページでも動画 ID が隠される。そこで、通常の YouTube にある同じアルバムのプレイリストから ID を補う。補った ID は、曲情報（タイトル・長さ・公式音源かどうか）で正しい曲か確かめてから、「Premium限定」として候補に加える。ログインしていない状態では「動画を再生できません」としか返らない公式音源も、ブラウザでは Premium 限定と表示されるため、「Premium限定（推定）」として同じに扱う。扱いは設定で選べる。

アルバムごと再生できない状態に見える作品（YouTube 側に収録曲のプレイリストが無い）や、検索にもアルバムにも出てこない曲は、この方法では見つからない。そうした曲は Web 検索で Topic 動画を探し、`add-video` で候補に加える。

- `include`（既定）：通常の曲と同じく自動一致させる
- `review`：要確認に回す
- `exclude`：その曲しか一致しない場合はスキップする

一致する候補が 1 件、または複数あっても同じ音源（長さの差 2 秒以内。シングルとアルバムの両方に収録されている場合など）なら自動で決める。複数の候補の中ではアルバム名が一致するものを優先する。それ以外は「要確認」、似た曲すら無い場合は「見つからない」として保留する。

## 安全性

- 照合（曲の検索）は**ログインしていない状態**で行うため、その間にアカウントの内容が変わることはない
- アカウントに書き込むのは `Liker.like()`（`rate_song(videoId, "LIKE")`）と `Liker.follow()`（`subscribe_artists`）だけ。前者は適用画面で `yes` と入力したとき、または `apply --execute` を実行したとき、後者は `follow --execute` を実行したときだけ呼ばれる。高評価やフォローの解除、プレイリストの変更は一切しない
- 適用前に、すでに高評価済みの曲を取得し、それらには API を呼ばない（一覧の取得に失敗したときは警告を出し、ローカルの記録だけで重複を防ぐ）。付与した曲は 1 曲ごとに記録し、同じ videoId には二度付けない
- 3 回連続で失敗したら中断する（もう一度 `x` を押すと続きから再開する）
- Spotify でお気に入りに追加した日時が古い曲から順に高評価を付ける。そのため、YouTube Music でも Spotify と同じく、最近追加した曲が上に並ぶ

## 開発

```sh
uv run pytest
```

テストはネットワークを使わない（検索・高評価はフェイクに差し替えている）。

| ファイル | 役割 |
| --- | --- |
| `src/ytm_import/spotify_csv.py` | Exportify の CSV の読み込み |
| `src/ytm_import/textnorm.py` | 正規化、曲名の版表記の解析、アーティスト名の照合 |
| `src/ytm_import/matcher.py` | 候補の採点と自動判定 |
| `src/ytm_import/ytm.py` | YouTube Music の検索・認証・高評価 |
| `src/ytm_import/store.py` | SQLite による状態の保存 |
| `src/ytm_import/runner.py` | 照合・適用の実行（UI から独立） |
| `src/ytm_import/tui.py` | Textual による TUI |

### 技術選定

| 技術 | 選んだ理由 |
| --- | --- |
| Python + [ytmusicapi](https://github.com/sigma67/ytmusicapi) | YouTube Music の非公式 API として最も保守されており、検索結果から長さ・音源の種類（`MUSIC_VIDEO_TYPE_ATV`）・アルバムを構造化して取得できる。TypeScript の選択肢（youtubei.js など）より YouTube Music 固有の機能の実績が多い |
| [Textual](https://textual.textualize.io/) | 表・モーダル・バックグラウンド処理を備えた TUI フレームワーク |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | 文字列の類似度の計算 |
| SQLite（標準ライブラリ） | 1 曲処理するたびに結果を保存するので、どこで止まってもそれまでの結果は失われない |

なお、ytmusicapi 1.12 の `search(filter="songs")` は、言語を日本語にすると結果が空になる（検索結果の区切りの見出し「曲」を、英語の "song" と比較しているため）。そこで、検索リクエストの送信とパースは `ytm.py` で行っている。

## 免責事項

- 作者が自分の移行のために作った個人用のツールです。動作の保証やサポートはしません。不具合の報告や改善の提案は歓迎しますが、対応するとは限りません。
- YouTube Music の非公式 API（[ytmusicapi](https://github.com/sigma67/ytmusicapi)）と、YouTube の内部 API を使っています。Google・YouTube・Spotify とは関係がなく、各サービスの仕様が変わると動かなくなることがあります。
- 各サービスの利用規約を確認したうえで、自分のアカウントに対して自己責任で使ってください。このツールの利用によって生じた損害（アカウントへの影響を含む）について、作者は責任を負いません。

## ライセンス

[MIT](LICENSE)

# 愛知アート帳 - 運用セットアップ手順

## 全体構成

```
[GitHub Actions（毎日7:00 JST）]
   → scripts/update_events.py が artscape・原画展.info を取得
   → data/events.json を更新してコミット
   → index.html と data/ をGitHub Pagesへ公開
[スマホ・PCのブラウザ]
   → 公開URLを開く（開いた日の日付で表示）
```

## 初回セットアップ（1回だけ）

1. GitHubで新しいリポジトリを作る: https://github.com/new
   - Repository name: `aichi-art-tracker`
   - Public を選ぶ（GitHub Pagesの無料プランは公開リポジトリが必要）
   - 「Add a README file」「.gitignore」「license」は**すべて選ばない**（空のまま作成）
2. このフォルダの内容をGitHubへ送る（`git push`。実行前に確認が入る）。
3. リポジトリの Settings → Pages → Build and deployment → Source を **GitHub Actions** にする。
4. Actions タブ → Update and deploy → Run workflow で1回手動実行する。
5. 成功すると `https://lastradezanpano.github.io/aichi-art-tracker/` で公開される。

## 日々の運用

- 何もしなくても毎朝自動で更新される。
- 失敗が続いていないかは、リポジトリの `data/scrape_status.json` か Actions タブで確認する。
- コラボカフェ等の新規追加が必要なときは、Claude Codeで `update_prompt.md` の手順を実行し、`data/events.json` を更新して `git push` する。

## うまくいかないとき

- Actionsが赤（失敗）: Actionsタブでログを開く。多くは情報源サイトの構造変更。`scrape_status.json` の `error` に原因が出る。
- 情報源がGitHubのクラウドIPをブロックする場合: ローソンチケットと同様に自動化できない。その場合は該当情報源だけClaude Codeでの手動補完に切り替える。
- 公開ページに古いデータが出る: ブラウザの再読み込み。データは毎回最新を取得する設定になっている。

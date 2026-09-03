# doc-style-skill

[![skills.sh](https://skills.sh/b/jomatsu/doc-style-skill)](https://skills.sh/jomatsu/doc-style-skill)

自分のブログや記事から、文体を再現するAgent Skillを作ります。
生成したskillには文体ルール、チェックリスト、文章リンターが含まれます。

## インストール

Claude Code、Codex、Cursorなど:

```bash
npx skills add jomatsu/doc-style-skill
```

インストーラーが追加先のエージェントを確認します。グローバルに追加する場合:

```bash
npx skills add jomatsu/doc-style-skill -g
```

Piのpackageとして追加する場合:

```bash
pi install https://github.com/jomatsu/doc-style-skill
```

インストール後、新しいエージェントセッションを開始してください。

## 使い方

ブログや著者ページのURLを渡して、文体スキルの作成を依頼します。

```text
自分のブログ https://example.com と
Zenn https://zenn.dev/your-name の記事から文体スキルを作ってください。
```

Piから明示的に呼び出す場合:

```text
/skill:author-style-builder 自分のブログ https://example.com から文体スキルを作ってください。
```

エージェントが記事一覧の取得、本文の整理、文体分析、skillの生成と検証まで進めます。
途中で提示される文体ルールの候補を確認し、採用するものを選んでください。

ローカルのMarkdownやTXTから作ることもできます。

```text
./articles にある自分の記事から文体スキルを作ってください。
```

他の著者を対象にする場合は、本人の明示的な許可が必要です。

## 生成されるもの

```text
author-skill/
├── SKILL.md
├── references/
├── lint-config.json
└── scripts/lint.sh
```

生成したskillを使って記事を書いた後は、同梱リンターで確認できます。

```bash
bash author-skill/scripts/lint.sh --text draft.md
```

## 開発

```bash
# Sudachiあり
uv run --with pytest --with sudachipy --with sudachidict-core \
  pytest skills/author-style-builder/scripts/tests

# fallback
uv run --with pytest pytest skills/author-style-builder/scripts/tests
```

実装と詳細なワークフローは
[`skills/author-style-builder/SKILL.md`](skills/author-style-builder/SKILL.md)を参照してください。

## License

[MIT](LICENSE)

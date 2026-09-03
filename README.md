# doc-style-skill (author-style-builder)

特定の日本語著者（技術記事・ブログ・エッセイなど）の文章コーパスを分析し、その文体を再現するエージェント用スキル（author スキル）を生成・検証・保守するためのメタスキルツールキットです。

本リポジトリは、AIエージェントが解釈できる`SKILL.md`、自動検証リンター、チェックリストを生成します。

---

## 主な特徴

1. **IR ファースト（中間表現を経由する決定論的コンパイル）**
   - 分析結果をプロンプトに直接記述するのではなく、実測根拠に基づく Claim-Evidence 中間表現（`profile.json`）として構造化します。
   - `profile.json` から決定論的に author スキル（`SKILL.md`、`lint-config.json`、`references/`）をコンパイルします。手動での直接編集によるドリフトを防ぎます。

2. **頻度から命令への直訳禁止（分布とレンジの保持）**
   - 計量結果（文長、読点頻度、品詞比率など）を「常に〜せよ」といった単純な命令文に変換しません。
   - 中央値、四分位範囲（IQR）、95% ブートストラップ信頼区間（CI）などの統計分布として保持し、「文長の中央値は 30〜45 字が中心」といったレンジ表現としてスキル化します。

3. **厳格な安全・倫理ゲート（Safety & Consent First）**
   - 対象著者の明示的な同意または権利許諾が確認できない場合、パイプラインの実行を停止（abstain）します。
   - なりすまし、世論誘導、フィッシング等への悪用を防止するため、命令優先度において安全・権利・事実保持が文体模倣よりも常に優先されます。

4. **散文契約（Prose Contract）に基づく公平な文体計量**
   - 技術記事特有のコードブロック、表、見出し、インラインコード、URL、ボイラープレートを正確に分離します。
   - 本文の散文（prose）のみを同一基準で抽出し、インラインコードや URL 密度を文体特徴と誤認させない前処理契約をすべての解析・検証スクリプトで共有しています。

5. **35 の形態素チャネルと統制された統計較正**
   - SudachiPy による形態素解析を用い、品詞 n-gram、機能語 bigram、文末接尾辞、助動詞分布、形式名詞率など 35 種類の形態素チャネルを抽出します。
   - 記事単位のFWER（Family-Wise Error Rate）1%を基準に、複数ゲートの偽陽性率を管理しながら検証閾値を較正します。

6. **スタンドアロンの検証リンターを同梱**
   - 生成された各 author スキルには、依存関係を自己解決して任意の作業ディレクトリから実行できる検証ランナー（`scripts/lint.sh`、`style_lint.py`、`overlap_check.py`）が同梱されます。
   - G1〜G7の個別ゲートにより、分布・文末・表記・語彙・過剰反復・原文との重複・形態素分布を評価します。

---

## 設計思想と命令の優先順位

本ツールキットによって生成されるスキル、およびビルダー自身は、以下の絶対的な命令優先順位に従います。下位の規則が上位の規則を上書きすることは禁止されています。

```
安全・同意・権利
  > ユーザー内容・事実
    > 意味保存
      > 出力形式
        > モード制約（ブログ / 解説 / エッセイ等）
          > コアスタイル
            > 表層の好み
              > 例示
```

---

## ワークフロー（Phases 0〜7）

author スキル構築パイプラインは、中断と再開が可能な 8 つのフェーズで構成されます。進捗は各ワークスペースの `STATE.md` に記録されます。

```
Phase 0: 前提ゲート (同意・許諾の確認)
   ↓
Phase 1: コーパス取得・intake (raw スナップショット、ブロック分類、重複排除)
   ↓
Phase 2: コーパス分割 (70% train / 15% dev / 15% hidden holdout)
   ↓
Phase 3: 特徴分析 (定量: 記事単位分布・形態素較正 / 定性: 談話コードブック)
   ↓
Phase 4: profile.json 構築 (Claim 候補の安定性検定と承認)
   ↓
Phase 5: スキルコンパイル (決定論的レンダリング、リンター同梱)
   ↓
Phase 6: 検証・リリース (静的リント、発火テスト、holdout リンター、judge 評価)
   ↓
Phase 7: 運用・反復改善 (Loop A: 編集差分 / Loop B: 月次微修正 / Loop C: 凍結回帰)
```

### Phase 0: 前提ゲート（必須）
- ユーザー自身の文章であるか、本人の明示同意または権利許諾が存在するかを確認します。
- 同意証拠レベル（`self_attested`, `direct_record`, `authorized_delegate`, `user_reported`）を特定し、`manifest.json` に記録します。確認できない場合は即座に停止（abstain）します。

### Phase 1: コーパス取得・intake
- テキストを取り込み、`raw/` 配下に不変スナップショット（SHA-256）として保存します。
- 本文、引用、コードフェンス、インデントコード、見出し、ボイラープレートを分離し、健全性チェック（`block_health`）を実施します。
- 転載や改訂の重複クラスタを検出し、著者本人が執筆した記事（subject-authored）のみを分析対象とします。

### Phase 2: 分割（Train / Dev / Holdout）
- 記事（重複クラスタ）単位・時系列順に 70% train / 15% dev / 15% hidden holdout に分割します。
- 最新記事群は holdout として最終検証まで厳重に凍結（非開封）します。文単位での分割やリークは禁止されています。

### Phase 3: 分析（定量 & 定性）
- **定量分析**: 記事ごとの文長、段落長、読点頻度、文末形式、文字種比率、および 35 チャネルの形態素分布を算出します（`extract_features.py`）。
- **定性分析**: 談話コードブック（`discourse-codebook.md`）に沿って修辞技法や論理展開をラベル付けし、記事 ID と文字オフセットによるエビデンスを付与します。
- **安定性検定**: 3 記事・2 層以上で支持され、ブートストラップ 70% の方向一致、LOAO（Leave-One-Article-Out）で反転しない特徴のみを core claim 候補とします。

### Phase 4: profile.json 構築
- 安定性検定を通過した候補（`profile-candidates.json`）を人間がレビューし、採用する claim を `profile.json` に登録します。
- 支持が弱いものは `mode_specific`, `local`, `ambiguous` に降格し、未承認の曖昧な claim は core に含めません。

### Phase 5: コンパイル
- `compile_skill.py` により、テンプレートをもとに author スキル一式をレンダリングします。
- トークン予算（SKILL.md 全体 ≤ 5,000 tok / 常時ロード部 ≤ 2,000 tok）を厳守し、生の長大なコーパス文は含めず、合成作例または短い抜粋のみを配置します。
- スタンドアロンで動作する実行スクリプト群（`lint.sh`, `style_lint.py`, `overlap_check.py`, `lib/`）をスキルの `scripts/` ディレクトリにコピーします。

### Phase 6: 検証・リリース
- **ゲート 1（静的リント）**: スキル仕様の充足、claim の完全性、内部リンク切れ、生コーパス引用の有無を検査（`skill_lint.py`）。
- **ゲート 2（発火テスト）**: 正しい依頼で発火し、類似の別依頼やなりすまし依頼を確実に拒否するかを検証。
- **ゲート 3（決定的指標）**: 凍結 holdout 記事を一度だけ開封し、同梱リンター（G1〜G7 ゲート）で評価。
- **ゲート 4 & 5（LLM Judge & 人間監査）**: 複数系列モデルによるアンサンブル評価と、日本語ネイティブによるブラインド監査を実施。

### Phase 7: 運用・反復改善
- **Loop A（日常）**: ユーザーによる生成文の事後編集差分を `feedback_intake.py record` で記録。
- **Loop B（月次・10本ごと）**: 蓄積されたフィードバックから差分を分析し、人間承認を経て profile を微修正。
- **Loop C（四半期・モデル更新時）**: 凍結回帰テスト（`regression_run.py`）を実行し、文体ドリフトを監視。

---

## ディレクトリ構造

```
doc-style-skill/
├── LICENSE                         # MIT ライセンス
├── README.md                       # 本ドキュメント
└── skills/
    └── author-style-builder/       # 著者スタイル生成メタスキル本体
        ├── SKILL.md                # メタスキルの定義と運用プロトコル
        ├── references/             # 仕様書・設計リファレンス
        │   ├── corpus-acquisition.md # コーパス取得・法的ガイドライン
        │   ├── feature-catalog.md    # 定量文体特徴（Tier 1〜3 + 35 形態素チャネル）
        │   ├── discourse-codebook.md # 定性談話分析コードブック
        │   ├── profile-schema.md     # Claim-Evidence (profile.json) スキーマ
        │   ├── compile-rules.md      # スキル変換・決定論的レンダリング規則
        │   ├── eval-protocol.md      # 検証・リリースゲート仕様 (G1〜G7, judge, 監査)
        │   └── state-template.md     # 進捗記録用 STATE.md テンプレート
        ├── scripts/                # 自動化スクリプト一式 (PEP 723 インラインメタデータ)
        │   ├── ARCHITECTURE.md     # スクリプト群の内部アーキテクチャ仕様
        │   ├── corpus_intake.py    # コーパス取り込み・ブロック分類・重複クラスタリング
        │   ├── corpus_split.py     # 時系列・記事単位分割 (train/dev/holdout)
        │   ├── extract_features.py # 特徴量抽出・分布集計・形態素参照較正
        │   ├── stability_test.py   # 安定性検定・claim 候補生成
        │   ├── compile_skill.py    # author スキル生成・リンター自己同梱
        │   ├── style_lint.py       # 生成文の文体・安全性個別ゲート検査 (G1〜G7)
        │   ├── overlap_check.py    # 原文コーパスとの類似度・逐語コピー検査 (G6)
        │   ├── skill_lint.py       # 生成スキルの静的リント・整合性検証
        │   ├── feedback_intake.py  # ユーザー事後編集フィードバックの収集・集計
        │   ├── regression_run.py   # 凍結回帰テスト実行
        │   ├── lib/                # 共有ライブラリモジュール群
        │   │   ├── blocks.py       # 散文契約・ブロック分類
        │   │   ├── tokenize.py     # 文分割・Sudachi/フォールバック解析器
        │   │   ├── features.py     # 特徴抽出コアロジック
        │   │   ├── morph.py        # 35 形態素チャネル抽出・較正
        │   │   ├── calibration.py  # FWER 1% 較正ポリシー
        │   │   ├── claims.py       # Claim スキーマ検証・ゲート写像
        │   │   ├── stats.py        # ブートストラップ・効果量・統計関数
        │   │   └── io_utils.py     # I/O ユーティリティ・SHA-256 ハッシュ
        │   └── tests/              # pytest テストスイート (192 テスト)
        └── templates/              # 生成される author スキルの骨格テンプレート
            └── author-skill/
                ├── SKILL.md.template
                ├── lint-config.json.template
                ├── eval/activation-cases.yaml.template
                ├── meta/profile-ref.json.template
                ├── meta/provenance.json.template
                └── references/
                    ├── checklist.md.template
                    ├── examples.md.template
                    └── style-rules.md.template
```

---

## 必要要件とセットアップ

### 必要環境
- Python 3.10 以上
- [uv](https://github.com/astral-sh/uv)（推奨）

各 Python スクリプトは PEP 723 に準拠したインラインスクリプトメタデータを持っており、`uv run` を使用することで依存ライブラリ（`sudachipy`, `sudachidict-core` 等）が自動的に管理・実行されます。

### フォールバックモード
`sudachipy` が利用できない環境や直接 `python3` で実行された場合でも、自動的に正規表現ベースのフォールバックモードで動作します（品詞依存の形態素チャネルはスキップされ、文分割や文字種比率などの表層特徴で評価を継続します）。

### Skills CLIでインストール

```bash
npx skills add jomatsu/doc-style-skill
```

インストーラーが対応するエージェントを検出し、`author-style-builder`を追加します。
グローバルへ追加する場合は`-g`を指定します。

```bash
npx skills add jomatsu/doc-style-skill -g
```

### Piのpackageとしてインストール

```bash
pi install https://github.com/jomatsu/doc-style-skill
```

インストール後、新しいエージェントセッションでメタスキルを起動できます。

```text
/skill:author-style-builder
```

---

## 使い方

インストール後、新しいPiセッションでブログや著者ページのURLを渡します。

```text
/skill:author-style-builder 自分のブログ https://example.com と
Zenn https://zenn.dev/your-name の記事から文体スキルを作ってください。
```

skillは記事一覧の発見、本文取得、変換、キャッシュ、分析まで進めます。対象範囲や
著者本人からの依頼であることなど、判断が必要な点は会話の中で確認します。

手元にMarkdown/TXTがある場合は、ローカルのファイルやディレクトリを入力として
渡すこともできます。

```text
/skill:author-style-builder ./articles にある自分の記事から文体スキルを作ってください。
```

skillは会話の中で次の処理を進めます。

1. 対象者本人からの依頼か、利用許可があるかを確認する
2. コーパスを取得・整理し、記事単位で分析する
3. 文体ルールの候補と根拠を提示する
4. 人間が承認した候補からauthor skillを生成する
5. 生成したskillとリンターを検証する

文体ルールの候補は自動採用されません。エージェントから候補を提示されたら、採用・保留・
却下を選択してください。生コーパスや分析用workspaceは生成したskillへ含まれません。

既存skillの更新や再検証も、対象のworkspaceまたはskillを指定して依頼します。

```text
/skill:author-style-builder /path/to/workspace の文体スキルを新しい記事で更新して再検証してください。
```

各スクリプトを個別に実行する場合は、
[`SKILL.md`](skills/author-style-builder/SKILL.md)と
[`scripts/ARCHITECTURE.md`](skills/author-style-builder/scripts/ARCHITECTURE.md)を参照してください。

---

## テストの実行

リポジトリ内の全テスト（192 件）を実行し、散文契約、較正ポリシー、コンパイル規則、リンターゲートが正常に機能していることを検証できます。

```bash
# Sudachi 形態素解析器を含めた完全テスト (192 passed)
uv run --with pytest --with sudachipy --with sudachidict-core pytest skills/author-style-builder/scripts/tests

# フォールバックモードテスト (187 passed, 5 skipped)
uv run --with pytest pytest skills/author-style-builder/scripts/tests
```

---

## 倫理的利用・免責事項

本ソフトウェアは、著者が自身の執筆支援を行うこと、または明示的な権利許諾を得た正規のプロジェクトにおいて文体の一貫性を保つ目的で設計されています。

- **なりすましの禁止**: 実在する第三者を詐称してコンテンツを公開する行為、政治的説得・世論誘導、スパム・フィッシングサイトの生成への利用は固く禁止されています。
- **著作権・人格権の尊重**: 公開されている文章であっても、本人の承諾のない無断クローリングや無断模倣はお控えください。
- **内容の正確性**: 生成された文章の事実性、安全性、最終的な文責は、プロンプトを発行し承認した人間に帰属します。

---

## ライセンス

本プロジェクトは [MIT License](LICENSE) の下で公開されています。

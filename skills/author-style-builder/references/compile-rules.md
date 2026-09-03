# compile-rules — profile.json → author スキル変換規則

コンパイルは決定的に行う(同じ profile からは同じスキルが生成される)。
テンプレートは `../templates/author-skill/` を使う。

## 写像規則

| claim(status / compilation_target) | 変換先 |
|---|---|
| core / persona | SKILL.md のペルソナ段落(著者の文体を 3〜5 文で要約)。**persona claim が無ければ合成しない**(「未登録」と明記し、aggregate から文体要約を作らない) |
| core / always_on_rule | SKILL.md の常時ルール(箇条書き) |
| core / conditional_rule(condition あり) | SKILL.md の「条件付きルール(core)」節(条件を明示。常時ルールに混ぜない) |
| core / conditional_rule(condition なし) | 常時ルール扱い |
| mode_specific / conditional_rule | references/style-rules.md のモード別セクション |
| local / example | references/examples.md の few-shot 正例 |
| 失敗次元 / negative_example | references/examples.md の負例(違反理由ラベル付き) |
| 測定可能 claim / validator | lint-config.json のゲート設定(`morph.*` は G7_morphology へ) |
| `morph.*` を metric に持つ claim | validator / checklist / example のみ。persona / always_on_rule / conditional_rule へは**写像しない(exit 2)** |
| ambiguous / checklist | references/checklist.md の注意項目 |
| ambiguous / conditional_rule | 探索的プロファイル限定で SKILL.md の探索的ルール節(下記例外)。本番では**未描画**とし、profile-ref の excluded に理由つきで記録 |
| inferred・quarantined | **コンパイル禁止**(excluded に理由つきで記録) |

### 完全性(exit 2)

observed・非 quarantined の全 claim は `meta/profile-ref.json` の mappings か excluded の
どちらかに必ず載る。validatorは、style_lintが実際に評価しcompileが具体的な閾値を設定する
metricだけを写像できる。近いゲート名だけへの名目的写像は禁止する。写像先が無いclaim、
較正不能metric、evidence spanを読めないexample、`morph.*`の文面化が1つでもあれば
compile_skillは**出力を書かずexit 2**で止まる。

### 較正split・feature schema

lint-config / lint-morphology の較正に使うaggregateは`extract_features.py --split
train+dev`(またはtrain / dev)のものに限る。`calibration_split`が`all`(holdout混入)
や欠落ならexit 1。現行は`feature_schema=2` / `channel_registry_version=2`。
profile claimのschema・analyzer・median/rangeをaggregateと照合し、再現不能なstale claimは
exit 2で拒否する。`--allow-stale-claims`は移行候補確認専用で、stale claimをexcludedへ
回しprovenanceに記録するため、本番リリースには使えない。

## 探索的プロファイルの写像(ambiguous / conditional_rule)

コーパス量ゲート未達等で core が成立していない探索的プロファイルでは、人間が承認した
観察を通常の文体傾向として利用する。次の**全条件**を満たす claim を SKILL.md の
`## 文体傾向`節へ描画する。

1. `profile.profile_class == "exploratory"` が明示されている
2. 人間承認メタデータ(`profile.approval.decided_by` / `decided_at` / `decisions`)がある
3. claim は `state=observed`、`status=ambiguous`、
   `compilation_target=conditional_rule`である

描画規則:

- ルール文は常時ルールと同じ決定的レンダラ・同じ階層順で生成する
- 条件と低確信度は各claimの`condition`・`status`・`confidence`で管理する
- `meta/profile-ref.json` に `SKILL.md#style_tendencies` → claim_id の対応を残す
- persona / always_on_rule / validatorへの昇格は人間レビューと昇格ゲートで扱う

成熟度情報はruntime promptと分離し、`meta/provenance.json`、`lint-config.json`、
コンパイル時の診断へ記録する。profileの制約・記事数・未完了評価はこの監査面で確認する。

persona / always_on_rule の claim が 0 件のときは`meta/profile-ref.json`へ空mappingを
作らず、非空のmappingだけを保持する。

## ルール文の書き方

- **rule_text を主文とし、定量レンジは補足として括弧書きで添える**:
  `rule_text="短い文を重ねる。" value.range=[25,35] metric=sent_len_median`
  → 「短い文を重ねる(記事ごとの文長中央値は 25〜35字 が中心)」。
  レンジから文面を発明しない(「hi×2 字を超える文を連続させない」のような上限の創作は禁止)。
  `morph.*` の claim はレンジ補足も付けない(文面化しない)
- 「常に」「必ず」を使わない。「〜が多い」「〜を基本とし、△△の場合は例外」
- 各ルールの文末に claim_id は書かない(対応は meta/profile-ref.json に保持)
- 上位層(profile-schema.md の 7 層)のルールを先に、下位層を後に配置

## few-shot 規律

- プロンプト内: 正例 2〜6・負例 0〜2。初期レシピ =
  構造 1 + 談話/レジスター 1 + 局所形式 1 の正例 3 つ
- 負例は失敗次元 1 つにつき 1 つ。合成またはライセンス済みのみ。
  違反理由を明示ラベル(例: 「✗ 文末が単調: です。です。です。」)
- 例示バンク(example-bank): claim/モードあたり正例候補 5〜10 を
  workspaces 側に保持し、スキルへは選抜のみ載せる
- **生コーパスの長い引用をスキルに入れない**。例は短い span か合成に限る
- 順序・件数は実験変数。Phase 6 の dev 評価でスイープする

## トークン予算と配置

- 生成 SKILL.md: ≤5,000 tok / 500 行。常時ロード部(ペルソナ+コアルール+安全):
  ≤2,000 tok
- 命令 50〜70% / 例示 30〜50%
- 重要ルール(安全・優先順位・コアスタイル)は冒頭に、
  セルフチェックリスト参照は末尾に配置(Lost in the Middle 対策)
- モード別ルール・例・チェックリストは references/ に分離し、
  発火後に必要なものだけ読む構造にする(progressive disclosure)

## モードルータ仕様(生成 SKILL.md に埋め込む)

1. ユーザー指定(明示メタデータ: 「ブログ向けに」等)を最優先
2. 指定がなければタスク内容から意味分類(対象媒体・ジャンルの手がかり)
3. 判定に自信がなければ**共有 core(中立寄り)で書き、ユーザーに確認**
4. 観測外モード(unobserved)を要求されたら、その旨を伝えて core で代替
5. スタイル強度(intensity)の指定を受け付ける:
   弱 = core の常時ルールのみ / 標準 = + モードルール / 強 = + 例示の積極適用

## 生成時の必須メタデータ

- `meta/profile-ref.json`: ルール文 ↔ claim_id の対応表(監査用)
- `meta/provenance.json`: 生成元 profile のバージョン・コーパスハッシュ・
  rights_scope・生成日時・メタスキルのバージョン
- `lint-config.json`: dev セット分布から算出した各ゲートの閾値
  (Phase 3 スクリプト実装までは手動計測値でよい。算出方法を必ず記録)

## リンター閾値の較正原則

共通実装は`scripts/lib/calibration.py`。holdoutを見ず、記事単位のhard-fail family-wise
error上限を**1%**に事前登録する。

- warn: 上限型p95、下限型p05、両側p10/p90またはmedian±IQR。線形補間分位点を使う
- hard: n<100では著者極値をフル精度で保存し、スカラーはTukey fenceとの合併。
  境界一致・JSON丸めだけで較正記事をfailさせない
- n>=100では、fail可能なlegacy検査とG7チャネルの総数に対するBonferroni型の
  外側順序統計量を使う。両側検査はalphaを両tailへ分割する。独立p01/p99は使わない
- G1/G2-form/G3はwarnとhardを分離し、文数・文字数が少ない入力はhard failではなく
  degradedにする。G2連続・G4も同じ共通hard境界を使う
- G5は人間承認済みmarkersがある場合だけhard gate。空なら`skipped(no_markers)`であり、
  production skillのskill_lintは不合格。aggregateから口癖を捏造しない
- G6はstoplist実効長と段落containmentで散文同士を比較する
- G7は記事IDを除いた参照を`lint-morphology.json`へ書く。warn上限チャネルはfailにせず、
  合成スコアを作らない。インラインコード/URLプレースホルダは全チャネルから除外
- Sudachi較正をfallbackで評価しない。G2/G4/Sudachi依存G7は
  `analyzer_mode_mismatch`としてdegraded/skipped

## 同梱リンター

生成スキルは `scripts/lint.sh` と style_lint 一式(lib/ 含む)を同梱し、
`meta/provenance.json#runner` に sha256 を記録する。lint.sh は自身の位置から
lint-config.json / lint-morphology.json を解決するので任意の cwd から実行できる。
SKILL.md のワークフローはこの lint.sh を呼ぶ(builder のパスに依存しない)

## 生成 SKILL.md の description 要件

- positive trigger: 「〇〇の文体で書く / 書き直す / 添削する」
- negative trigger: 汎用校正・翻訳・本人を騙る用途では発火しない旨を明記
- 同意の範囲(rights_scope)を frontmatter コメントに記載

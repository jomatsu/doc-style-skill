# profile.json スキーマ — Claim-Evidence 中間表現

1 claim = 1レコード。レコード履歴はappend-only、現行ビューはマージ済み。

## レコード形式

```json
{
  "claim_id": "sent-end-001",
  "category": "構造 | 談話 | 文 | 語彙 | 表記 | 文末",
  "scope_mode": "core | <mode-id>",
  "condition": "発火条件。core なら null(例: 'ジャンル=エッセイ')",
  "rule_text": "自然言語での観察。例: 段落末を体言止めで締めることが多い",
  "feature": {
    "schema": "2",
    "analyzer": "sudachipy==0.6.x",
    "dictionary": "sudachidict_core==YYYYMMDD",
    "split_mode": "C",
    "metric": "指標名(feature-catalog.md のキー)",
    "denominator": "分母の定義(文数 / 文字数 / 段落数)"
  },
  "value": {
    "median": 0.2,
    "range": [0.12, 0.28],
    "effect_size": 0.6,
    "ci95": [0.1, 0.31]
  },
  "evidence": [
    {"article_id": "a012", "char_start": 340, "char_end": 385, "note": "任意"}
  ],
  "support": {"articles": 5, "strata": 2, "bootstrap_agreement": 0.84},
  "control_result": {
    "masking": "pass | fail | not_run",
    "cross_topic": "pass | fail | not_run",
    "loao": "pass | fail | not_run"
  },
  "agreement": {"kalpha": 0.72, "disagreement_records": []},
  "state": "observed | inferred | unobserved",
  "status": "core | mode_specific | local | ambiguous | quarantined",
  "compilation_target": "persona | always_on_rule | conditional_rule | example | negative_example | validator | checklist",
  "rights_scope": "同意記録の参照(例: consent-record-id)",
  "confidence": "high | medium | low",
  "version": "1.0.0",
  "history": ["変更ログ(旧レコードへの参照)"],
  "usage": {
    "adherence_rate": null,
    "violations": 0,
    "feedback_support": 0,
    "feedback_contradiction": 0,
    "last_reviewed": null
  }
}
```

`usage`は任意フィールド(Loop A/Bの運用統計)。
feedback_contradiction が feedback_support を上回り続ける claim は
Loop B で降格・レンジ再較正の候補にする。

## フィールド規律

- **feature.schema**: その数値を抽出したFeatureRecordのスキーマ版。現行は`"2"`。
  profileルートにも`"feature_schema": "2"`を記録する。旧版・欠落時は現行aggregateで
  数値再現性を検査し、driftがあればcompile exit 2
- **feature.metric**: feature-catalog.md のキー。形態素チャネルは `morph.<channel>`
  (例: `morph.funcword_bigram`)。**`morph.*` を持つ claim の compilation_target は
  validator / checklist / example のみ**。persona / always_on_rule / conditional_rule
  に付けると compile_skill が exit 2 で拒否する(形態素分布を文面の命令にしない)
- **compilation_target=validator**: metric はstyle_lintが実際に評価し、compileが具体的な
  閾値を設定するゲートに限る。近いゲート名だけへの名目的写像は禁止。対応が無い・
  aggregateで較正不能ならexit 2
- **condition**: core + conditional_rule で condition があれば SKILL.md の
  「条件付きルール(core)」節へ描画される(常時ルールには混ぜない)。condition が
  空なら常時ルール扱い
- **evidence**: 最低 1 span 必須。span は記事 ID + 文字オフセットで指す。
  原文の長い引用を profile に貼らない(コピー防止)。
- **state**: `observed`(コーパスで実測)のみコンパイル可。
  `inferred`(推測)は隔離。`unobserved`(そのモードでの観測なし)は abstain 条件に。
- **agreement**: 談話系(category=談話/構造)claim に必須。
  Krippendorff α が低いものは status を ambiguous へ降格。
  正当な不一致は disagreement_records に残し、単一ラベルに潰さない。
- **完全性**: profile に採用した observed・非 quarantined の全 claim は、生成スキルの
  `meta/profile-ref.json` の mappings か excluded(理由つき)のどちらかに必ず載る。
  黙って落とされる claim は無い(compile_skill exit 2 / skill_lint `--profile` で検査)。

## 昇格ゲート(status 判定)

| 条件 | status |
|---|---|
| 3 記事・2 層支持 + bootstrap 70% 方向一致 + LOAO 反転なし + masking/cross_topic 通過 | core |
| 特定モード内でのみ上記を満たす(モードは 5 記事かつ 1 万字以上) | mode_specific |
| 支持不足だが代表例として価値がある | local(example のみ) |
| 方向不安定・低一致 | ambiguous(validator / checklist / abstain へ) |
| 矛盾・権利不明・injection 疑い | quarantined(コンパイル禁止) |

**ambiguous はどの経路でも本番の core に入らない**。昇格は上表のゲートを再度通すことでのみ行う。

## feature schema移行

旧profileを新しいfeature schemaのaggregateへ黙って流用しない。compile_skillはclaimの
median/rangeをaggregateのCI/IQRと照合し、再現不能なclaimを`stale`としてexit 2で列挙する。

```bash
uv run scripts/stability_test.py --workspace <ws> \
  --out profile-candidates-migration.json
```

候補を人間レビューしてprofileへ採用する。`compile_skill --allow-stale-claims`はstale claimを
excludedへ回す確認用escape hatchで、provenanceにmigrationを記録する。この生成物は
skill_lintが本番不合格にする。

## profile レベルのフィールド(探索的プロファイル)

コーパス量ゲート未達などでcoreが1件も作れない場合、人間承認の上で
 profile ルートに次を置く:

```json
{
  "profile_class": "exploratory",
  "approval": {"decided_by": "human requester", "decided_at": "YYYY-MM-DD", "decisions": ["..."]},
  "policy": {"core_allowed": false, "mode_specific_allowed": false, "max_confidence": "low"},
  "limitations": {"corpus_size": "...", "controls": "..."}
}
```

- `profile_class` の既定は `production`(未設定も同義)。`exploratory` は人間が明示的に
  付与し、`approval`を伴うことで成立する。成熟度・制約は`limitations`と
  `meta/provenance.json`で監査する。
- 探索的プロファイルでは `ambiguous` の claim に `compilation_target=conditional_rule` を
  許容する。これは生成スキルの`文体傾向`節へ描画される。
  具体的な描画規則はcompile-rules.mdの「探索的プロファイルの写像」を見る。
- この例外は status を昇格させない。**ambiguous claim は本番の core
  (persona / always_on_rule)には決して入らない**。

## マージ規則

- 等価 claim(同一 metric・同一 scope で範囲が重なる)はマージし、
  最良 evidence(support が最大のもの)を代表に置換。旧レコードは history へ。
- カテゴリあたり現行 claim は最大 10。超過時は effect_size と support の弱い順に
  local へ降格。

## 階層(7 層)と優先順位

上位層を下位層が上書きすることを禁止:

1. 安全・権利・内容忠実(claim 化しない。常にハードゲート)
2. 記事アーキテクチャ(構成・見出し・冒頭/結びの型)
3. 談話・段落運び(遷移・例示・対比・呼びかけ)
4. 文リズム・統語(文長分布・係り受け傾向・読点)
5. 語彙・機能フレーズ(助詞・助動詞・口癖)
6. 表記・句読点(漢字かな比・全半角・送り仮名)
7. 文末・レジスター(です・ます/だ・である/体言止め)

## モード構造

- 共有 author-core + 条件付きモード(媒体 / ジャンル / 読者 / 時期)。
- モード成立は「5 記事かつ 1 万字」以上。未満は core へ縮約するか、
  claim に `unobserved` を明示して abstain。
- 経年ドリフトは era モードとして分離し、共有 core へ shrinkage。
- strata(ジャンル・記事型・主題等)と era(時期)は直交軸。eraだけの差を
  core昇格に必要な「2 strata」支持へ数えない。
- 単一媒体でも事前規則で複数strataを設計できる。1 strataしかない場合は
  core条件未達として exploratory / mode_specific / ambiguous へ降格する。

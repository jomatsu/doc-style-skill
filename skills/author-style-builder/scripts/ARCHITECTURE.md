# scripts アーキテクチャ仕様

実装者(人間・エージェント)は本仕様に従う。

## 実行方式

- 各スクリプトは PEP 723 インラインメタデータを持ち、`uv run <script>.py` で実行
  (依存: sudachipy, sudachidict-core。requires-python >=3.10)
- `python3` 直接実行で sudachipy が無い場合は**フォールバックモード**で動作:
  正規表現ベースの文分割 + 文字種特徴のみ。POS 依存特徴は `null` を出力し、
  結果 JSON に `"analyzer": {"mode": "fallback"}` を明記
- 乱数を使う処理(bootstrap 等)は `--seed`(既定 42)で決定的に
- 全スクリプトは `--help` を持ち、終了コード: 0=成功 / 1=エラー / 2=ゲート不合格

## データレイアウト(workspaces/<author-id>/)

```
raw/<article_id>.txt          # 不変スナップショット(UTF-8 プレーンテキスト)
clean/<article_id>.json       # {"article_id", "blocks": [{"type", "text", "char_start", "char_end"}]}
                              #   type: body | quote | code | boilerplate | editorial
manifest.json                 # {"author_id", "consent": {...}, "articles": [ArticleMeta]}
splits.json                   # {"train": [...], "dev": [...], "holdout": [...], "created_at", "leak_check": {...}}
features/<article_id>.json    # FeatureRecord(下記)
features/_aggregate.json      # 較正 split(train+dev)の集計(分布・CI・形態素チャネル参照)
profile.json                  # {"author_id", "version", "claims": [Claim]}(profile-schema.md 準拠)
feedback/fb-*.json            # 利用時フィードバック(Loop A)。report.json は集計候補
eval/                         # 評価ログ
STATE.md                      # 進行記録(人間可読)
```

ArticleMeta: `{"article_id", "canonical_url", "retrieval_timestamp", "license",
"consent_record", "content_hash"(sha256), "status", "authorship", "strata",
"published_at"(ISO日付, 不明なら null), "char_count", "block_health"}`。
`block_health={"status":"pass|warn|fail","metrics":{...},"errors":[],"warnings":[]}`。
manifest.consent は record/granted に加え evidence_level
(`self_attested|direct_record|authorized_delegate|user_reported|none`)と
verification_status を持つ

## lib/ モジュール契約

### lib/blocks.py(散文契約 — intake / extract / lint / overlap が共有)

- `parse_frontmatter(text) -> (meta, body_start)` / `classify_blocks(text, body_start)`
  / `classify_text(text)`: raw → body / quote / code / boilerplate / editorial。
  frontmatter・見出しは boilerplate、フェンス(不正ネスト回復つき)・4 スペース
  インデントは code。オフセットは raw 基準
- `check_block_health(blocks)`: body 空・フェンス/インデントコード残存・コード様行
  過剰を検出(intake の quarantine 判定)
- `prose_segments(blocks, mode="placeholder"|"drop") -> [Segment]`: body ブロック →
  散文セグメント。テーブル行・単独行 URL・画像/HTML のみ行・`:::` を除外、箇条書きは
  マーカーだけ落として本文を残す、インラインコードと文中 URL は名詞プレースホルダ
  `識別子`(placeholder)または削除(drop、G6 用)。`raw_map` / `masked` を持ち
  `raw_span(segment, s, e)` で raw 座標へ戻せる
- `prose_document(text)` / `prose_text(text)`: G6 用の正規化散文(+ raw 座標写像)
- **契約**: 同じ散文からは extract_features(clean blocks 経由)と style_lint
  (raw テキスト経由)が同一の FeatureRecord を得る。コード・表・見出し・単独行 URL
  の有無は形態素統計を変えない(tests/test_lib.py::TestProseContract)

### lib/tokenize.py

- `get_analyzer() -> Analyzer` : sudachipy(mode C)を試み、失敗時 fallback
- `Analyzer.meta() -> dict` : `{"mode": "sudachi"|"fallback", "version", "dict", "split_mode"}`
- `split_sentences(text) -> list[Sentence]` : 「。！?!?」+ 改行で分割。
  `Sentence = {"text", "char_start", "char_end"}`
- `Analyzer.tokenize(text) -> list[Token]` :
  `Token = {"surface", "pos"(大分類), "pos_detail", "base", "pos_full", "cform", "start", "end"}`。
  fallback では空リスト

### lib/features.py(extract と lint で共有)

- `extract_article_features(blocks, analyzer) -> FeatureRecord`
  ブロック列 → `blocks.prose_segments` → 文レコード(`build_sentences`)→ 特徴。
- `record_from_text(text, analyzer) -> FeatureRecord`: raw テキストから同じ経路で算出
  (style_lint / feedback_intake 用。article_id は None)
- FeatureRecord(feature-catalog.md のキーに対応):

```json
{
  "article_id": "...", "feature_schema": "2", "analyzer": {...}, "n_sents": 0, "n_chars": 0,
  "sent_len": {"median": 0, "iqr": [0,0], "max": 0},
  "para_len": {"median": 0, "iqr": [0,0]},
  "comma_per_sent": {"median": 0, "iqr": [0,0]},
  "sent_end_form": {"desu_masu": 0.0, "da_dearu": 0.0, "jotai_verb": 0.0, "jotai_adj": 0.0, "taigen": 0.0, "question": 0.0, "other": 0.0},
  "max_consecutive_same_ending": 0,
  "script_ratio": {"kanji": 0.0, "hiragana": 0.0, "katakana": 0.0, "latin": 0.0, "digit": 0.0, "other": 0.0},
  "func_word_rate": null, "particle_bigram": null, "pos_bigram": null,
  "aux_verb_dist": null, "ttr_window": null, "distinct_2": null,
  "prose": {"n_segments": 0, "n_list_segments": 0, "n_masked_inline": 0, "max_consecutive_span": [0, 0]},
  "morph": {"available": false, "n_tokens": null, "n_sents": 0,
            "dist": {"<channel>": {"key": prob} | null}, "scalar": {"<channel>": float | null},
            "sample": {"<channel>": n}}
}
```

- 文末形式判定: 文末の助動詞/表層から desu_masu(です・ます・でした等)/
  da_dearu(だ・である等)/ jotai_verb(常体の動詞・助動詞終止)/ jotai_adj(形容詞終止)/
  taigen(名詞・助詞止め)/ question(?・か)を分類。fallback では表層正規表現で近似
- `n_chars` は散文文字数(プレースホルダ区間を除く)。`max_consecutive_span` は raw 座標
- `feature_distance(a, b, keys) -> dict` : 分布間距離(lint 用、Jensen-Shannon)

### lib/morph.py(形態素チャネル。validator 専用)

- `CHANNELS`レジストリ(`CHANNEL_REGISTRY_VERSION=2`): 分布19 + スカラー16 = 35。
  一人称はlemma分布/最頻lemma集中度として著者非依存に定義し、形式名詞「気」を除外
- `extract_morphology`: fallbackではsurfaceチャネルのみ計算。`masked=True`の
  インラインコード/URLプレースホルダを全チャネル・分母から除外
- 較正: 分布warn=LOAO p90、スカラーwarn=p10/p90。hardは`lib/calibration.py`の
  記事単位FWER 1%ポリシー(小nはフル精度極値、大nはBonferroni外側順序統計量)
- 評価はstatus/distance|value/percentile/top_deviationsを返す。warn上限チャネルは
  failへ格上げせず、min_sample未満はskipped
- チャネルはSKILL.mdへ描画しない。`morph.*`の文面化はexit 2

### lib/calibration.py / lib/claims.py

- `calibration.py`: G1〜G4/G7共通のwarn/hard境界、記事単位FWER、フル精度比較、
  退化帯警告を定義
- `claims.py`: 実際に評価されるmetricだけの意味的ゲート写像、feature schema/analyzerと
  claim.value対aggregateのdrift検査をcompile/skill_lintで共有

### lib/stats.py(純 Python、numpy 不使用)

- `bootstrap_ci(values, n=1000, seed) -> {"median", "ci95": [lo, hi]}`
- `bootstrap_direction_agreement(author_vals, ref_vals, n=1000, seed) -> float`
- `cliffs_delta(a, b) -> float`(効果量)
- `loao_stable(values, direction_fn) -> bool`

### lib/io_utils.py

- manifest / splits / features / profile の load・save(スキーマ軽検証つき)
- `content_hash(text) -> str`(sha256)

## CLI 契約

corpus_intake / corpus_split / extract_features / stability_test / compile_skill /
feedback_intake は `--workspace <path>` を必須引数とする。style_lint / overlap_check /
skill_lint / regression_run は生成スキルだけで動く(workspace は任意)。
regression_run の golden 結果には `failed_gates`(style_lint で fail したゲート名)を
付ける。`--with-copy-check` は pass golden にも G6 を適用するため、pass golden が
著者実記事(生コーパス由来)のワークスペースでは定義上 G6 が fail する。
この場合は閾値の劣化ではないことを `failed_gates == ["G6"]` で切り分ける。

| スクリプト | 引数 | 動作 |
|---|---|---|
| corpus_intake.py | `--input <dir|file>...` `--author-id` `[--consent <ref> --consent-level <level>]` | txt/md を取込み raw/ 保存、ブロック分類(見出し・コードフェンス・4スペースコード・引用行)、block_health検査、完全+近似重複検出→クラスタ化、manifest 更新。consent欠落またはhealth failはstatus=quarantined |
| corpus_split.py | `--ratio 70,15,15` | published_at(なければ retrieval)順に記事単位分割。転載クラスタは同一 split。8-gram 跨割リークチェック。splits.json 出力 |
| extract_features.py | `--split train|dev|train+dev|all`(既定 train+dev) | eligible+subject-authored のみ対象。features/*.json と _aggregate.json(記事単位分布・bootstrap CI・等記事/等文字重み両方・形態素チャネル参照 `morphology`: global centroid/LOAO 閾値 + register/length/era 条件付き参照(N>=10 built / 5..9 shrunk / <5 skipped))を出力。`all` は holdout を含むため警告し、compile_skill はその aggregate を拒否する |
| stability_test.py | `--feature <key>...` `[--baseline <json>]` | _aggregate から候補 claim を生成。既定baselineは工学的初期値で、baseline_sourceを出力。3記事・2層 / bootstrap 70% / LOAO を判定。masking・対照著者検定はnot_run。profile-candidates.jsonのみ出力(自動採用しない) |
| compile_skill.py | `--profile` `--templates` `--out <dir>` `[--now]` `[--allow-stale-claims]` | claimを決定的に描画。split/schema/registryを検査し、数値claimがaggregateで再現不能ならexit 2。validatorは実ゲートへの意味的写像のみ許可。`--allow-stale-claims`は移行確認専用でstaleをexcludedにし、生成物をrelease-block状態にする。G1〜G4/G7を共通FWERポリシーで較正し、G5 markerはprofile由来のみ。同梱runnerとprovenanceを生成 |
| style_lint.py | `--config lint-config.json` `--text <file>` `[--source-corpus <dir>]` `[--era YYYY]` | 散文契約でG1〜G7を非合成評価。短すぎる入力はdegraded。G5 marker空は`skipped(no_markers)`、G6 source未指定はskipped。Sudachi較正をfallbackで実行するとG2/G4/Sudachi依存G7は`analyzer_mode_mismatch`でdegraded/skipped。G7はチャネル別status/percentile/deviation/span/worst_sliceを報告 |
| overlap_check.py | `--text <file>` `--against <dir>` | 両者を散文正規化してから exact(25字連続、stoplist 実効長)/ 文字 5-gram Jaccard / MinHash(128 perm)/ 段落 containment を報告。埋め込み類似は `--embeddings` 指定時のみ(未指定なら skipped と明記)。全コーパス copy-index は deferred と明記 |
| skill_lint.py | `--skill <dir>` `[--profile <json> | --workspace <ws>]` `[--source-corpus <dir>]` `[--no-runner-smoke]` | 静的リント、claim完全性、意味的写像、schema/registry、claim drift、migration release block、production G5 marker、runner hash/別cwd smoke、生コーパス引用を検査 |

## compile_skill.py の探索profile写像

`profile.profile_class == "exploratory"`の場合、次の契約で承認済みclaimを扱う
(規格の正本は`../references/compile-rules.md`、フィールド定義は`profile-schema.md`):

- `approval.decided_by / decided_at / decisions`をコンパイル入口で検証する
- `collect_exploratory_claims(profile, compilable)`で`state=observed`、
  `status=ambiguous`、`compilation_target=conditional_rule`を集める
- `apply_exploratory_block()`が`SKILL.md.template`のマーカー区間を`文体傾向`として描画する
- ルールは常時ルールと同じ`render_rule_text`・階層順で決定的に並べる
- `meta/profile-ref.json`の`SKILL.md#style_tendencies`へclaim_idを写像する
- profile class、approval、limitationsは`meta/provenance.json`と`lint-config.json`へ記録する

## 生成スキルの同梱リンター

compile_skill が `<skill>/scripts/` に `lint.sh` / `style_lint.py` / `overlap_check.py` /
`lib/{blocks,features,morph,calibration,claims,stats,tokenize}.py` をコピーする(`RUNNER_FILES`)。
`lint.sh` は自身の位置から `../lint-config.json` を解決するため任意の cwd から動く。
`uv` があれば PEP 723 で sudachipy を解決、無ければ `python3`(fallback)。
`STYLE_LINT_PYTHON` で解釈系を固定できる。`PYTHONDONTWRITEBYTECODE=1` を立て、
スキル dir に `__pycache__` を書かない。sha256 は `meta/provenance.json#runner.files`。

## テスト

`scripts/tests/` に pytest 一式と合成コーパス(`tests/fixtures/synthetic/`、
著作権フリーの自作短文25記事に加え、常体中長文・mixed/list-heavy・n=120の
プログラム生成コーパスと性質テストを置く。Sudachi時**192件**。
fallback: `uv run --with pytest pytest scripts/tests`(187 passed, 5 skipped)。
sudachi: `uv run --with pytest --with sudachipy --with sudachidict-core
pytest scripts/tests`(192 passed)。全パイプラインのスモークが通ること:
intake → split → extract(train+dev)→ stability → compile → skill_lint →
style_lint(G1〜G7)→ feedback / regression。主な契約テスト:

- `test_lib.py::TestProseContract` — コード・表・見出し・URL が形態素統計を変えない、
  インラインコードの決定的プレースホルダ、raw 座標写像
- `test_pipeline.py::TestProseContract` — clean blocks 経由と raw 経由の FeatureRecord 一致
- `test_compile_lint.py::TestCompileRules` — core 条件付き描画、production の ambiguous
  除外、写像不能 claim の exit 2、合成ペルソナ禁止、レンジ補足の非発明、`morph.*` の
  文面化禁止、較正 split の許容値
- `test_compile_lint.py::TestMorphologyGate` — 較正記事・holdout 記事が G7 で fail しない、
  読点位置(fallback でも動く)・機能語・文末 suffix を崩した文で該当チャネルが反応、
  warn 上限チャネルは fail しない、参照欠落は明示 skipped
- `test_compile_lint.py::TestStyleLint` — 散文 50% 逐語コピーは G6 fail、無関係散文は pass

## 禁止事項

- profile.json / manifest.json の破壊的変更(常にバックアップ or 追記)
- コーパス本文をエラーメッセージ・ログに長く出力(50 字まで)
- ネットワークアクセス(全スクリプトはオフラインで動く)
- split=all の aggregate で較正・コンパイルする(holdout 混入)
- 形態素チャネル(`morph.*`)を SKILL.md の文面へ描画する

# feature-catalog — 日本語定量スタイル特徴カタログ

全特徴は**記事を統計単位**として算出し、分布(中央値・IQR)+ 効果量 +
95% bootstrap CI で profile.json に記録する。点推定を命令にしない。

## 前処理の固定

- 解析器: SudachiPy + SudachiDict(分割モード C を既定)。バージョンをピンし、
  claim の `feature.analyzer` / `feature.dictionary` に記録
- Unicode 正規化(NFKC)ポリシーを固定。生テキストと正規化テキストは並行保持
- 長文記事は自然境界(見出し → 段落 → 文)で決定的にチャンクし、二重計上を防ぐ
- 集計は等記事重み・等文字重みの両方で感度を確認
- **feature schema**: 現行`FEATURE_SCHEMA_VERSION=2`。FeatureRecord・aggregate・profile claim・
  生成provenanceに記録し、版や解析器が異なる数値の黙示的流用を禁止
- **散文契約**(`scripts/lib/blocks.py`): 統計対象は body ブロックから作った散文
  セグメントのみ。コードフェンス・4 スペースコード・見出し・テーブル行・単独行 URL・
  画像/HTML のみ行・Zenn `:::` は除外し、箇条書きはマーカーだけ落として本文を残す。
  インラインコードと文中 URL は名詞プレースホルダ `識別子` に決定的に置換する。
  プレースホルダ由来トークンは文字数・語彙特徴だけでなく**全形態素チャネルの
  分子・分母から除外**し、識別子密度を文体と誤認しない。
  extract_features(clean blocks)と style_lint(raw テキスト)は同じ契約を通るので、
  同じ散文からは同じ FeatureRecord が得られる

## Tier 1 — 優先(トピック非依存性が高い)

| キー | 定義 | 分母 | 備考 |
|---|---|---|---|
| func_word_rate | 機能語率(助詞・助動詞・接続詞・形式名詞) | 総形態素数 | 一研究で比較 4 特徴中最良(人間/AI 判別タスク)。要自コーパス検証 |
| func_phrase | 機能フレーズ頻度(「のではないか」「ということ」等) | 文数 | J-STAGE 研究で有効性確認 |
| particle_bigram | 助詞 bigram 分布 | 助詞総数 | |
| pos_ngram | POS bigram / trigram 分布 | 総形態素数 | |
| comma_position | 読点位置(文節境界からの相対位置分布) / `comma_per_sent_median` = 各文の「、」「，」個数を数え、その**記事内中央値**を取る | 文数 | 平均読点数ではない。中央値0でも「記事全体で読点なし」を意味しない |
| sent_len | 文長分布(文字数の中央値・IQR・最大) | — | |
| para_len | 段落長分布(文数) | — | |
| sent_end_form | 文末形式分布: desu_masu / da_dearu / jotai_verb(常体の動詞・助動詞終止)/ jotai_adj(形容詞終止)/ taigen(体言・助詞止め)/ question / other | 文数 | ジャンル効果大 → 条件付きモードで扱う。fallback は表層近似 |
| sent_end_repeat | 同一文末形式の連続数(最大・平均) | — | リンター G2 の cap 元 |

## Tier 2 — 有効(条件付き)

| キー | 定義 | 備考 |
|---|---|---|
| script_ratio | 漢字 / ひらがな / カタカナ / ラテン / 数字の文字比率 | |
| orthography | 表記ゆれ規則(全半角・送り仮名・かな書き傾向: 「事/こと」「出来る/できる」等) | 個別語は語彙リストとして |
| aux_verb | 助動詞分布(「〜だろう」「〜かもしれない」等モダリティ含む) | ヘッジ/断定の定量面 |
| ttr / distinct_n | 語彙多様性(移動窓 TTR、distinct-1/2) | 生成の多様性下限チェックに使用 |
| punct_style | 句読点スタイル(「、。」vs「,.」、三点リーダ・ダッシュ・かっこの使い方) | |
| loanword_rate | カタカナ語・英字語の使用率 | トピック影響に注意。masking 検定必須 |

## Tier 3 — conditional(core 昇格禁止)

| キー | 定義 | 理由 |
|---|---|---|
| dependency | 係り受け・文節構造特徴 | パーサ依存が強い |
| content_word | 内容語(名詞・動詞語幹)頻度 | トピック脆弱。POS マスキング後に生き残る場合のみ |
| topic_vocab | 分野固有語彙 | スタイルではなく主題。語彙リストは参考情報として保持可 |

## 形態素チャネル(morph.*、validator 専用)

`scripts/lib/morph.py` の `CHANNELS` レジストリ(`channel_registry_version=2`、
分布19 + スカラー16 = 35チャネル)。
記事ごとに分布(dist)またはスカラーを算出し、`_aggregate.json#morphology` に
著者参照を較正する。**SKILL.md の文面(ペルソナ・常時ルール)へは描画しない**。
compile_skill は `morph.*` を metric に持つ claim を validator / checklist / example
以外へ写像しない(exit 2)。スカラーの一部は checklist の観察候補になり得るが自動採用しない。

| チャネル | 種別 | 要件 | 最大重症度 | top_k | min_sample | 内容 |
|---|---|---|---|---|---|---|
| pos_unigram | dist | sudachi | fail | 20 | 50 | 品詞 unigram |
| pos_bigram | dist | sudachi | fail | 40 | 50 | 品詞 bigram |
| pos_trigram | dist | sudachi | fail | 60 | 80 | 品詞 trigram |
| particle_bigram | dist | sudachi | fail | 40 | 30 | 助詞 bigram(助詞列の隣接対) |
| funcword_bigram | dist | sudachi | fail | 60 | 40 | 機能語(助詞・助動詞・接続詞)bigram |
| aux_lemma | dist | sudachi | fail | 20 | 15 | 助動詞 lemma 分布 |
| final_suffix2 | dist | sudachi | fail | 40 | 8 | 文末 2 トークン(機能語は lemma、内容語は品詞へマスク) |
| final_suffix3 | dist | sudachi | warn | 60 | 10 | 文末 3 トークン |
| final_pos_cform | dist | sudachi | fail | 20 | 8 | 文末トークンの品詞×活用形 |
| content_masked_lemma_bigram | dist | sudachi | warn | 80 | 60 | 内容語を品詞にマスクした lemma bigram |
| formal_noun | dist | sudachi | warn | 7 | 5 | 形式名詞(こと・もの・ため・わけ・気・はず・準体助詞の)分布 |
| conj_lemma | dist | sudachi | warn | 20 | 5 | 接続詞 lemma 分布 |
| para_initial_pos | dist | sudachi | warn | 10 | 5 | 段落頭トークンの品詞 |
| para_initial_conj | dist | sudachi | warn | 15 | 3 | 段落頭接続詞 |
| pre_comma_pos | dist | sudachi | warn | 15 | 8 | 読点直前の品詞 |
| pre_comma_lemma | dist | sudachi | warn | 30 | 8 | 読点直前のマスク lemma |
| comma_rel_pos | dist | surface | warn | 4 | 8 | 読点の文内相対位置(四分位 q1〜q4) |
| hedge_class | dist | surface | warn | 8 | 3 | 日本語モダリティの事前リストによるヘッジ種別(だろう / かも / はず / ようだ / たぶん / と思う / 気がする)。特定著者の頻度順ではない |
| first_person_lemma | dist | sudachi | warn | 6 | 3 | 一人称lemma分布(特定の「自分」を基準にしない) |
| formal_noun_rate | scalar | sudachi | fail | — | 50 | 形式名詞率(/トークン)。こと・もの・ため・わけ・はず・準体助詞「の」。「気」は文脈なしで判別不能のため除外 |
| demonstrative_rate | scalar | sudachi | warn | — | 50 | 指示語率 |
| first_person_rate | scalar | sudachi | warn | — | 50 | 一人称率 |
| first_person_top_share | scalar | sudachi | warn | — | 3 | 一人称lemmaのうち最頻lemmaが占める割合 |
| quote_sentence_rate | scalar | surface | warn | — | 5 | 「」『』を含む文の比 |
| conj_rate | scalar | sudachi | warn | — | 5 | 接続詞数 / 文数 |
| para_initial_conj_rate | scalar | sudachi | warn | — | 3 | 段落頭が接続詞の比 |
| verb_noun_ratio | scalar | sudachi | fail | — | 50 | 動詞 / 名詞 |
| modifier_density | scalar | sudachi | fail | — | 50 | 副詞・形容詞・形状詞・連体詞 / トークン |
| comma_first_quarter_ratio | scalar | surface | warn | — | 8 | 読点のうち文の前 1/4 にあるものの比 |
| comma_rel_pos_median | scalar | surface | warn | — | 8 | 読点相対位置の中央値 |
| negative_rate | scalar | sudachi | warn | — | 5 | 否定(ない・ぬ・ん)を含む文の比 |
| past_rate | scalar | sudachi | warn | — | 5 | 「た」を含む文の比 |
| speculative_rate | scalar | surface | warn | — | 5 | 推量ヘッジを含む文の比 |
| hedge_rate | scalar | surface | warn | — | 5 | ヘッジを含む文の比 |
| max_consecutive_same_suffix2 | scalar | sudachi | warn | — | 5 | 同一 final_suffix2 の最大連続 |

較正・評価の規則:

- 距離は Jensen-Shannon 距離(sqrt(JSD)、底 2、0〜1)。参照 centroid は記事等重み平均を
  上位 `top_k` + OTHER に有界化したもの(質量保存)
- warnは分布距離p90、スカラーp10/p90。hard境界は`lib/calibration.py`の
  記事単位FWER 1%ポリシーを共有する。n<100はフル精度の著者極値(スカラーはTukey
  fenceとの合併)、n>=100は全fail可能検査へBonferroni補正した外側順序統計量。
  独立p01/p99を使わず、丸め値で比較しない。n<10はskipped
- 条件付き参照(register、length、era)はN>=10で独自較正、5..9でglobalへ縮約、<5でskipped。
  固定length境界とregister 0.5は情報用sliceであり、global gate判定には使わない
- `max_severity=warn`はfailへ格上げしない。Sudachi較正をfallbackで実行した場合、
  analyzer依存チャネルは`analyzer_mode_mismatch`でskipped。surfaceチャネルは参照と
  sampleが有効なら維持する。合成スコアは作らない
- 退化したwarn/hard帯やtop_kに対して疎なsampleはcompile warningへ出す
- 較正はtrain+dev(またはtrain/dev)のみ。`--split all`はcompile_skillが拒否する

## 安定性検定(全特徴共通)

core 昇格の条件(profile-schema.md と同一):

1. 3 記事以上・2 層以上で支持
2. 記事単位 bootstrap リサンプルの 70% 以上で方向一致
3. leave-one-article-out で方向が反転しない
4. content masking(トピック語・固有表現をマスク)後も差分が残る
5. 対照条件(同トピック他著者 / 同著者他媒体 / 中立リライト)を上回る

不合格 → mode_specific / local / ambiguous へ降格(棄却しない)。

## stability baseline の出所

`stability_test.py` の既定baselineは、対照コーパスから推定した値ではなく、
候補方向を作るための**工学的初期値**(`DEFAULT_BASELINE`)である。結果JSONの
`baseline_source` が `built_in_engineering_defaults` の場合、外部一般性を主張しない。

- 適切な同ジャンル対照コーパスがある場合は、値→数値JSONを作り
  `--baseline <json>` で差し替える。出力にそのpathを記録する
- 各claimの `baseline` と profile-candidates.json の `baseline_source` を保持する
- 工学的初期値との差だけで「著者固有」と断定しない。masking/cross_topicと
  人間承認を通し、対照未実施なら control_result=not_run のまま降格判断する

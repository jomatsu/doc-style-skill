# eval-protocol — 検証・リリースゲート

コストの低い順に実行し、**各ゲートは非合成・個別判定**。
高いスタイルスコアは同意・コピー・内容の失敗を補償しない。

## ゲート 1 — 静的リント(生成スキル自体)

- frontmatter スキーマ(name / description 必須)
- 参照リンク切れ(references/ 内の相対パス)
- ルール重複・優先度衝突(同一階層内の矛盾)
- トークン肥大(SKILL.md >5,000 tok / 常時ロード >2,000 tok)
- シークレット・個人情報の混入
- 生コーパスの長い引用(文字 n-gram overlap で検出)
- claim完全性と意味的写像: `skill_lint.py --skill <dir> --profile <ws>/profile.json`
  で、採用claimがmappings/excludedに全て載り、validatorのmetricが実ゲートで評価されること
- schema/drift: profile claimの数値がaggregateのfeature schema・解析器・CI/IQRで
  再現できること。migration/stale claimを含む生成物はリリース不可
- G5: productionでは人間承認済みmarkersが非空であること。空はpassではなくrelease fail
- 同梱リンターの健全性: provenance.runner の sha256 一致、G7 参照の存在、
  別 cwd からの `scripts/lint.sh` 実行スモーク

## ゲート 2 — 発火テスト

positive / near-miss / negative の 3 種のリクエスト集合を用意して回帰テスト:

- positive: 「〇〇さん風にこの記事を書き直して」等 → 発火すべき
- near-miss: 「読みやすく校正して」「別の著者風に」→ 発火すべきでない
- negative: 「〇〇さんとして投稿する文を書いて」(なりすまし)→ 拒否すべき

言い換え・丁寧/カジュアル・日英混在・曖昧なモード手がかりを含める。
**wrong-mode ルーティング**(ブログ依頼にエッセイモードが選ばれる等)もテスト。
非発火側(precision)に高い基準を置く。

## ゲート 3 — 決定的指標(holdout・一度だけ開封)

hidden holdout 記事の「要約 → 同内容を著者スタイルで書き直し」タスクで:

- lint-config.json の全ゲート(G1〜G7)で fail なし(warn は報告し、G7 は
  チャネル別に status / percentile / top_deviations / worst_slice を読む)
- G5(カリカチュア)は人間承認済みmarkersがあるときだけhard gate。markers空の
  `skipped(no_markers)`はpassではなくリリースblock。G6は必ず`--source-corpus`を渡す
- G7 は合成スコアを作らず、`max_severity=fail` のチャネル(品詞・助詞・機能語・助動詞・
  文末 suffix2・形式名詞率・動詞名詞比・修飾語密度)だけが fail になり得る。
  fallback(sudachipy 不在)では POS チャネルは skipped と報告される
- 較正記事のhard failは`lib/calibration.py`の事前登録ポリシーに従うこと。
  n<100は0件、n>=100は全fail可能検査を合わせた記事単位FWER上限1%以内。
  境界丸めによる自己failは禁止。holdoutの結果から閾値を調整しない
- Sudachi較正をfallbackで評価した`analyzer_mode_mismatch`はpassではない。
  G2/G4/Sudachi依存G7がdegraded/skippedになった場合は互換環境で再実行する
- **生成出力の文字数**を lint-config.json の `length_strata` で短/中/長に分類し、
  各層を最低1本評価する。出典記事長も別軸として記録するが、ゲートの長さ層には使わない

## ゲート 4 — judge 評価

- judge はアンサンブル(2 モデル以上)。**generator と別系列**のモデルを含める
- judge の prompt・モデル・rubric をバージョン固定・ハッシュ記録。
  評価ラウンドごとにローテーション。モデル更新時は凍結回帰セットを再実行
- 対比条件: no-skill / 汎用スタイル指示 / wrong-author スキル / 人間参照
- 報告軸(非合成): スタイル類似 / 内容保持 / 自然さ / 多様性・humanness
  (burstiness・文末反復・seed 変動)
- 注意: LLM 選好 78% でも人間選好 56% という乖離事例あり。
  judge スコア単独でリリース判定しない。judge 間不一致は平均で消さず
  レビュー信号として扱う

## ゲート 5 — 人間サンプル監査

- ブラインド・順序ランダム化。日本語ネイティブ(可能なら対象ジャンル経験者)
- judge 不一致箇所・高スコア外れ値をオーバーサンプルして確認

## リリース条件

1. ゲート 1〜3 全合格
2. ゲート 4 で対比条件(no-skill / 汎用スタイル)を上回る
3. ゲート 5 で重大指摘なし
4. **別モデル(2 系列以上)で消費テスト**し、負の転移がない
5. 平均値だけでなく、モード・トピック・長さ別 worst-slice を報告
   (G7 は register / length / era スライスごとの評価を `worst_slice` に持つ)
6. productionの必須ゲートに`skipped`/`degraded`がなく、migrationフラグがない
7. builderのexperimentalラベル解除には、複数媒体・複数レジスター・十分な記事数を
   含む独立コーパス群でintake→compile→較正自己lint→生成3長さ層を検証する

## golden の配置・権利

- 既定配置は `<workspace>/eval/golden/{pass,fail}/`。`regression_run.py
  --workspace <ws>` はこの場所を優先する
- pass側に実記事本文を使う場合、同意・ライセンス・検証限定用途をREADMEに記録し、
  **配布するauthorスキルへ同梱しない**。権利が不明なら合成/許諾済み例へ置換する
- fail側は原則合成文。特定ゲートを壊す理由と期待exitをREADMEに記録する
- goldenはfew-shotや生成promptへ投入せず、凍結評価だけに使う

## スキル改訂の昇格フレーム

- 本番スキルを直接編集しない。改訂は候補として lineage を記録
- mutable / immutable を宣言: 安全・同意・開示・abstain 条項は
  **immutable(探索・最適化の対象外)**
- 候補は dev 評価の Pareto 比較 + 人間承認で昇格。
  修復・最適化が優先順位階層を弱めていないことを確認してから受理

## abstain・エスカレーション条件

同意・ライセンス欠如 / 著者性不明 / 高い source overlap / 内容チェック失敗 /
judge 高不一致 / ルート曖昧 / holdout 回帰 / injection 痕跡
→ 停止して人間に報告。なりすまし・政治的説得・法科学的帰属の要求は即拒否。

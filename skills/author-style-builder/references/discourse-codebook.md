# discourse-codebook — 談話・修辞特徴コードブック

LLM は**ラベルの提案役**。すべてのラベルに evidence span(記事 ID + オフセット)と
確信度を付け、定量検証と人間確認を経てから claim 化する。
印象(「优しい文体」等)を頻度と混同しない。

## コード一覧

各コードは「値の候補」を持つ。記事ごとにラベル付けし、分布として集計する。

### A. 記事アーキテクチャ(階層 2)

| コード | 値の候補 | 例 |
|---|---|---|
| A1_opening | 問題提起 / 結論先出し / エピソード / 引用 / 定義 / 読者への質問 / 目的宣言 / 免責・自己開示 | 冒頭 1〜3 文で判定。目的宣言=「この記事では〜する」、免責・自己開示=経験不足・立場・前提を先に明かす |
| A2_structure | 直列(手順) / 対比 / 問題→解決 / 主張→根拠→例 / 時系列 | 見出し・段落構成から |
| A3_closing | まとめ / CTA / 余韻(体言止め・問いかけ) / 次回予告 / なし | 最終段落 |
| A4_heading_style | 名詞句 / 文 / 疑問文 / 番号付き | 見出しの型 |

### B. 談話・段落運び(階層 3)

| コード | 値の候補 | 例 |
|---|---|---|
| B1_example_density | 段落あたり具体例数(数値) | コード例・体験談・比喩を含む |
| B2_example_type | 体験談 / コード / 数値データ / 比喩 / 仮想例 | |
| B3_transition | 接続詞明示 / 暗黙 / 問いかけで繋ぐ | 段落間遷移の型 |
| B4_reader_address | 呼びかけあり(「皆さん」「〜してみてください」) / なし / 一人称共有(「〜してみましょう」) | |
| B5_contrast | 対比の使用頻度と型(「一方」「しかし」/ 譲歩「たしかに〜だが」) | |
| B6_meta_comment | 自己言及・脱線・括弧内ツッコミの頻度 | エッセイで重要 |
| B7_link_presentation | standalone_raw_url / markdown_link / inline_raw_url / none | standalone_raw_url は `https?://...` が空白を除き単独行を占める**機能**。Markdownリンクや文中URLとは別値。単に本文中に生URLがあるだけを bare URL と呼ばない |

### C. モダリティ・レジスター(階層 3〜7 と連動)

| コード | 値の候補 | 例 |
|---|---|---|
| C1_hedge | ヘッジ表現(「〜かもしれない」「〜と思う」「〜な気がする」)の密度 | 文数比 |
| C2_assertion | 断定(「〜である」「〜すべき」)の密度 | |
| C3_rhetorical_q | 修辞疑問の頻度 | |
| C4_humor | ユーモアの型: 自虐 / 誇張 / ツッコミ / なし | 低一致になりやすい → agreement 必須 |
| C5_emotive | 感情表現・感嘆の頻度(「!」「〜ですね」) | |

## ラベル付けプロトコル

1. **単位**: A 系は記事単位、B/C 系は段落または文単位でラベル → 記事単位に集計。
2. **evidence 必須**: 各ラベルに該当 span を最低 1 つ。span なしのラベルは無効。
3. **値辞書を先に固定**: 上表のcanonical valueだけを使う。新値が必要なら
   codebook_versionを上げ、一次/二次の両方を同じ辞書で再codingする。
4. **ダブルコーディング**: パイロットの20%(最低4記事)を独立に2回ラベル付け。
   各コードで共同ラベル単位10件以上を目標に Krippendorff α(nominal)を算出する。
   - α ≥ 0.67: そのまま使用
   - 0.4 ≤ α < 0.67: ambiguous 扱い(checklist 行きの候補)
   - α < 0.4: コード定義を見直すか廃棄
   - 共同ラベル単位 <10、定数列等でα算出不能: percent agreementとnを併記し、
     confidence=low。これだけを根拠にcore昇格しない
   正当な不一致は disagreement_records に保持。
5. **対照条件**: 同トピック他著者・同著者他媒体・中立リライトに同じコードを適用し、
   対照を上回る差分のみ claim 化。媒体テンプレート(編集部の型)は共変量として除外。
6. **claim 化**: 分布が安定性ゲート(3 記事・2 層・bootstrap 70%)を満たしたら
   profile-schema.md に従い claim へ。rule_text は観察として書く
   (「〜が多い」であって「〜せよ」ではない)。

## コーディング記録の共通JSON

一次・二次とも `eval/discourse-coding-<coder-id>.json` に同じ形で保存する。
`value` は上表のcanonical value(「高/あり/true」等の独自値は禁止)。

```json
{
  "schema_version": "1.0",
  "codebook_version": "1.1",
  "coder_id": "independent-coder-2",
  "independent": true,
  "items": [
    {
      "item_id": "a012:A1_opening:article",
      "article_id": "a012",
      "unit": "article",
      "char_start": 0,
      "char_end": 85,
      "code": "A1_opening",
      "value": "目的宣言",
      "confidence": "high",
      "note": null
    }
  ]
}
```

同一 `item_id + code` を機械joinして一致を計算する。二次コーダーには一次結果・
profile候補・採用判断を見せない。集計は `eval/discourse-agreement.json` に
code別の `n, kalpha, percent_agreement, disagreements` を保存する。

## 談話候補ファイル

候補は workspace 直下 `profile-discourse-candidates.json` に置く。

```json
{
  "schema_version": "1.0",
  "codebook_version": "1.1",
  "source_split": "train",
  "candidates": [
    {
      "candidate_id": "disc-a1-purpose-001",
      "code": "A1_opening",
      "canonical_value": "目的宣言",
      "rule_text": "冒頭で記事の目的を宣言することが多い",
      "evidence": [{"article_id": "a012", "char_start": 0, "char_end": 85}],
      "support": {"articles": 5, "strata": 2},
      "agreement_ref": "eval/discourse-agreement.json#A1_opening",
      "recommended_status": "core",
      "recommended_target": "always_on_rule"
    }
  ]
}
```

候補はprofileへの自動採用を禁止する。人間承認後にprofile-schemaへ写像する。

# STATE.md テンプレート — 中断耐性チェックポイント

STATE.md は最終報告ではなく、再開に必要なwrite-ahead logとして使う。
**各Phase末だけでなく、各サブステップ・外部取得batch・評価ゲート直後に即時更新する。**
長時間処理の開始前にも `running` と予定出力を書く。

```markdown
# STATE — <author-id>

- updated_at: <ISO-8601>
- current_phase: <phase/substep>
- status: pending | running | blocked | complete
- next_action: <再開時に最初に行う1操作>

## Phase 0
- consent_record: <本文でなく参照>
- consent_evidence_level: self_attested | direct_record | authorized_delegate | user_reported
- verification_status: verified | reported_unverified | missing
- scope/restrictions: ...
- checkpoint: complete | blocked

## Phase 1
- acquisition: <source/API/export, rate, cache, fetched/expected>
- conversion: <tool/version/config>
- intake: <articles/body chars/code chars/quarantined/health warn/fail>
- artifacts: <paths + hashes where useful>
- checkpoint: ...

## Phase 2〜7
- command_or_action: ...
- started_at/completed_at: ...
- inputs: ...
- outputs: ...
- result: pass | warn | fail | interrupted
- holdout_opened_at: <未開封なら null。一度記録したら消さない>
- next_action: ...

## Decisions
- <人間承認・例外・棄却理由を追記。過去記録を消さない>

## Known issues
- <未解決点と再現手順>
```

更新規律:

1. 外部取得はbatchごと、LLM codingは記事/割当単位、ゲートは各ゲート直後に保存。
2. 接続断・429・ツール失敗も `interrupted` と最後の確定出力を記録する。
3. 「実行予定」と「ディスクで確認済み」を分ける。存在・終了コード・hash等で検証する。
4. holdout開封日時、同意判断、人間承認はappend-onlyで、やり直し時も消さない。
5. 再開時はSTATEだけを信用せず、記載された成果物をディスクで確認してから重複実行を避ける。

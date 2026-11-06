# Saven statement adapter

`SAVEN:<account-number>` selects the adapter registered in
`src/external_providers.py`. Its parser is `src/saven_export.py`. Saven-specific
format recognition and transfer classification live there. Shared loading,
transit, planning, and console code consume institution-neutral data.

For setup, display labels, transfers, state, and real-transfer checks, see the
[external account guide](external-accounts.md).

## Supported export

- Export CAD transaction history to Downloads; arbitrary/GUID filenames work.
- Recognize `Your Transactions List`, an `Account Number:` preamble, and
  `Date,Description,Amount (CAD),Balance (CAD)`. The observed currency-only `(CAD)`
  ledger header is also supported.
- Account IDs are digit strings. Dates use `DD/MM/YYYY`; rows are newest first.
- Amounts have two decimal places. The final balance field can contain unquoted
  thousands separators.
- Match by format/account, then select the newest file modification time. Tied
  newest files are an error.
- Use the newest ledger balance after validating order and adjacent movements.
  Exports must contain continuous activity from the tracked balance.
- Interest, fee, and charge descriptions are excluded from transfer principal
  while remaining part of the actual balance.

The adapter returns common `ExternalExport` / `ExternalTransaction` objects.
Its encrypted cache remains `data/saven_balance.json`, with the same four fields
per account. Set `display_name: Saven` and `account_type: HISA` in settings if those
are the desired labels; the parser does not choose them.

Inspect one configured account through the shared importer:

```powershell
python scripts/import_external_balance.py --account "SAVEN:0001112223" --directory "$HOME/Downloads"
```

Add `--save` after verification. Normal runs and this utility share cache/evidence
persistence. Scheduled runs read saved external data and Questrade activity.

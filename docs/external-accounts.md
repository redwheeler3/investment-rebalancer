# External accounts

This is a Questrade rebalancer. Its brokerage API, account rules, cash ledger, and
trade model are Questrade-specific. The extension boundary is for external CAD
cash/savings institutions whose balances come from exports.

A statement adapter translates institution-specific data into common balances
and ledger rows. Valuation, transit tracking, allocation math, and planning use
that common data. Account names and type labels come from settings. The current
production adapter is [Saven CSV](#saven-statement-adapter).

## Settings select the adapter and console labels

Use a quoted `PROVIDER:<account-id>` target in `targets`, or in
`tactical_deployment.fixed_composition` with tactical deployment. The prefix
selects a registered parser. An external account with a zero target stays
tracked until you remove the target. GIC term targets such as `GIC:2Y` group
positions reported directly by Questrade and need no statement adapter or transfer route.

Describe each account in private `config/settings.yaml`:

```yaml
external_accounts:
  "SAVEN:0001112223":
    display_name: Saven
    account_type: HISA
    questrade_account:
      owner_name: Secondary
      account_type: Margin
```

The display fields supply external rows and transfer labels; they do not select
the parser or change investment rules. `questrade_account` identifies the real
broker endpoint: exactly one personal Margin account with that configured owner
label. Each external account needs a distinct endpoint.

An unsupported provider is an error, not an ordinary Questrade symbol. Keep the
target and route configured until its balance is zero and transfers are settled.
Switching external institutions requires its adapter/registration and settings,
not changes to the planner. Replacing Questrade is outside this project's scope.

## Export loading and cached balances

Local runs ask the selected adapter for an export in `~/Downloads`. The adapter
owns format recognition, account-ID validation, matching-file selection, and
normalization. See its notes for selection rules and supported layouts.

The shared loader validates the normalized CAD balance and source timestamps.
Without an export it reuses the saved balance with its original dates. An unchanged
hash does not advance dates; older source data are ignored. Missing initial
balances and malformed or discontinuous data are errors rather than zero balances.
There is no elapsed-age cutoff.

Loading status uses the configured name, export outcome, and last activity date.
Scheduled runs use cached external data without scanning Downloads; they still
read Questrade activity for reconciliation.

To inspect or save exports separately:

```powershell
python scripts/import_external_balance.py --directory "$HOME/Downloads"
python scripts/import_external_balance.py --directory "$HOME/Downloads" --save
```

Use `--account "PROVIDER:<account-id>"` to select one configured account and
`--file` instead of `--directory` to select an export. `--save` updates movement
evidence together with the cache; inspection without it writes neither. Seed
caches locally before publishing external targets to scheduled runs.

## Recognizing transfers

**Run locally and let private state sync before initiating a transfer.** Initial
observations establish baselines; historical movements do not become new transit.
A successful local report remembers displayed recommendations. Scheduled runs
reconcile that intent without creating unseen recommendations. No manual start
or pause flag is required.

An outstanding recommendation and linked Questrade account supply intent for new
cash movements through the intermediate bank. This is an inference, not an
end-to-end transfer ID. On the linked account, the ledger explains CAD cash
changes using posted transactions and executions. A remaining change in the
recommended direction counts provisionally, up to the outstanding amount.
An unrelated payment in that same direction can therefore be attributed to the
recommendation. Opposite-direction cash movements do not advance it.

When a recommendation starts, the ledger records any unexplained difference
between opening and current CAD cash after accounting for the day's activity.
If a deposit or withdrawal already visible in that balance posts later, its
activity row does not create transit or a second receipt for the new recommendation.

Recognized principal is capped across the whole recommendation:

`min(cumulative related source withdrawals, original recommended amount)`

An unexecuted recommendation is not money. Excess withdrawals are ordinary
outflows. Actual destination balances remain uncapped, including top-ups from
money already at the intermediate bank.

| $180,000 recommended | Treatment |
| --- | --- |
| Withdraw $165,000 | $165,000 in transit; no value for the unused $15,000. |
| Withdraw $200,000 | At most $180,000 in transit; $20,000 leaves the tracked portfolio. |
| Withdraw $165,000; receive $180,000 with a bank top-up | Clear $165,000 of transit and include the full receipt; $15,000 joins the portfolio once. |
| Withdraw $200,000; receive $180,000 and spend $20,000 | The tracked portfolio is $20,000 lower. |
| Actually receive all $200,000 | Use the full actual destination balance. |

History, tactical evaluation and accuracy use the full reported balances throughout reconciliation. New transfer instructions for one route can wait while earlier postings are being matched.

### Questrade to an external account

A statement deposit counts toward the active recommendation regardless of how
you funded it. Once receipts reach the recommended amount, the transfer block
clears automatically. For a $10,000 receipt with $6,000 from Questrade, transit
clears and the $4,000 bank contribution increases tracked household value. If
your bank supplied all $10,000, household value rises by the full amount.

For partial transfers, the most left to send is the recommendation minus the
larger of the amount already sent and the amount received, with a floor of
zero. A $10,000 recommendation with $6,000 sent and $4,000 received therefore
has $2,000 in transit and at most $4,000 left to send.

An unexplained CAD cash debit can establish transit before the EFT posts. The
transfer table shows that portion as `Sent (awaiting activity)`. A posted debit
confirms the same principal. Partial withdrawals leave the unused allowance
available, and excess withdrawals leave the tracked household. Transit is an external
household asset, not broker buying power. A new statement confirming receipts
replaces matched transit with the reported balance. A larger receipt can include
a bank top-up; exact amount equality is not required.

### External account to Questrade

An unposted CAD receipt clears the corresponding portion of transit. For
example, if the statement shows $10,000 withdrawn and the broker receives
$4,000, $6,000 stays in transit. A $12,000 receipt clears all $10,000 and adds
$2,000 of outside money. When the broker receives first, the recommendation
caps the provisional adjustment until the source statement arrives. The report
continues in all three cases.

If the cash was spent on shares before the run, the account's CAD executions
explain that spending, including reported fees. Posted trades and fills are
matched by broker trade day, security and side, so a later trade posting does
not charge the purchase twice. The broker cash shown in the report is the
actual balance. Unobserved movements that offset one another between checks
can only be inferred from their net change until additional evidence posts.

Process a new statement as soon as it shows the withdrawal, even before Questrade
receives the money. The lower external balance plus transit preserves tracked
value until receipt.

If Questrade receives first, an `Awaiting export` deduction offsets the stale
external valuation. A statement confirming the source withdrawal reconciles that
estimate. Interest or fees alone do not establish a withdrawal; the adapter marks
which ledger rows are transfer evidence.

A source withdrawal confirmed by the current statement is authoritative. Larger
broker receipts are treated as outside-bank top-ups. Another external withdrawal
after that export needs newer data to identify it. Missing source data cannot
reliably distinguish those possibilities.

## Lifecycle and saved state

The original cap survives repeated reports and partial movements. A receipt awaiting its source statement consumes that much of the cap when sizing new instructions. Late statement rows dated before intent advance the bank balance and observation counters without becoming principal for that recommendation. The adapter supplies the statement timezone for this comparison. Unused intent
is dropped if the local plan drops it, or replaced if its direction changes. A
partly settled cycle retains its remaining cap even when residual drift falls
below tolerance. That allowance is internal bookkeeping, not an asset/display row.

The ledger remembers cash debits already observed for completed deposits until the EFT rows post. A late posting confirms those dollars even if another recommendation has started. A new cash movement is evaluated against the current recommendation. An opposite-direction recommendation can replace
a settled partial cycle. A route with unsettled money cannot be changed or removed.
No timer/expiry applies. The API's 31-day query limit is not display retention.

State is SOPS ciphertext in the private repo:

- Each adapter selects a balance-cache filename. The common account mapping has
  exactly `balance_cad`, `last_transaction_date`, `source_modified_at`, and
  `source_sha256`. Names/type labels stay in settings.
- `data/transfer_tracking.json` stores recommendation context, cumulative integer
  cents, and movement signature counts. Identical-looking repeated transactions
  retain multiplicity; polls and overlapping queries do not replay them.

Reconciliation precedes history and tactical evaluation. Source-read failures do
not produce those updates from an unreconciled snapshot.

## Console and projected funding

Holdings groups external balances below brokerage cash. Account Summary uses the
same household scope. Both show raw balances plus transit assets or negative
source adjustments, so their totals reconcile. Cash/position cells for external
and adjustment rows are blank.

External-bound transit counts toward its destination allocation, avoiding an
artificial drift alert. Questrade-bound transit is uninvested until its projected
arrival and buys. Projection can use expected arrivals as it uses planned FX;
actual broker cash changes only when the broker reports it.

Transfer tables use configured names/types. A single-name table is named for that
account; mixed names use `External Transfers`. Rows contain both endpoints, CAD
amount, and status. A broker cash receipt shows as `Received` until activity
reconciles it. Fully reconciled rows disappear and an empty table is omitted.
Symbols display as
`PROVIDER:...<last-four>` while matching uses full identifiers.

## Adapter contract

Add a parser and an entry in `src/external_providers.py`. Registration provides
ID validation, export discovery/reading, and a cache filename. It returns common
`ExternalExport` / `ExternalTransaction` objects: CAD balance, dates, source
identity, continuous ledger amounts/balances, and adapter-owned `is_transfer`
flags. Headers, descriptions, interest/fee recognition, and file formats belong
in the adapter. Names/types belong in settings.

`src/external_accounts.py` owns shared configuration/loading. The fictitious
non-CSV adapter in `tests/test_external_provider_boundary.py` verifies that loading,
tracking, planning, and rendering work without changing the Questrade core.

See [architecture](architecture.md#external-account-boundary) and
[planner reconciliation](rebalancer.md#automatic-transit-reconciliation).

## Reading the implementation

Follow the external-account path in this order:

1. `models.py`: `ExternalExport` and `ExternalTransaction` define adapter output;
   `TransferProgress` names the valuation amounts passed to planning/display.
2. `external_providers.py` and `external_accounts.py`: select the parser from the
   symbol prefix, validate settings, refresh caches, and attach reported balances.
   Loading also saves new ledger evidence; it can write state.
3. `transfer_tracking.py`: `observe_external_balance` and `observe_broker` count
   new movements; `progress` calculates each cycle's amounts; `apply` adjusts
   household valuation before history and tactical evaluation.
4. `rebalancer.py`, `portfolio.py`, and `fx_math.py`: carry expected arrivals
   through the proposed trades, projected allocation, and currency funding.
5. `display.py` renders the result. After the local report succeeds, `main.py`
   calls `remember_recommendations` to establish intent for future observations.

The tracker's persisted counters are **integer cents**; portfolio/planner amounts
are **CAD dollars**. `DEPOSIT` means into the external account and `WITHDRAW` means
out to Questrade. A recommendation cycle saves the cumulative counters at its
start, then measures later movements against those baselines. Its source amount
is capped, while actual receipts remain uncapped. Signature counts distinguish
repeated transactions from repeated observations of the same transaction.

`tests/test_transfer_tracking.py` walks through partial/excess amounts, either
arrival order, repeat imports, valuation, funding, and display. The fictitious
adapter in `tests/test_external_provider_boundary.py` demonstrates which code a
second institution needs and which shared code stays the same.

## Saven statement adapter

`SAVEN:<account-number>` selects the adapter registered in
`src/external_providers.py`. Its parser is `src/saven_export.py`. Saven-specific
format recognition and transfer classification live there. Shared loading,
transit, planning, and console code consume institution-neutral data.

### Supported export

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

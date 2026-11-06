# Saven balances and transfers

Saven is an external CAD holding in the household portfolio. Its reported balance
comes from a CSV export; Questrade positions, cash, and posted activity come from
the broker API. Transit tracking connects those observations without changing
reported account balances or making unreceived money available for trading.

## Configuration and first run

Include a quoted `SAVEN:<account-number>` in `targets`, or in
`tactical_deployment.fixed_composition` when using tactical deployment. Membership
selects tracked accounts, including zero-weight entries. Keep the account and
route configured until its balance is zero and its transfers are settled.

Configure its broker endpoint in the private `config/settings.yaml`:

```yaml
external_transfer_accounts:
  "SAVEN:0001112223":
    owner_name: Secondary
    account_type: Margin
```

The owner/type must identify exactly one personal Questrade Margin account. Each
Saven account requires a distinct endpoint so a broker movement has one route.

**Run locally and let private state sync before initiating a transfer.** Initial
observations establish baselines; historical withdrawals are not treated as new
transfers. A successful local report automatically remembers displayed transfer
recommendations. Scheduled runs reconcile existing intent but do not generate
recommendations the user has not seen. There is no manual start or pause flag.

## CSV selection and balance refresh

Local runs look in `~/Downloads` for Saven-format CSVs matching the configured
account. Names can be arbitrary. Selection uses the newest file modification
time, not a date in the filename; tied newest files are an error.

The parser validates the CAD ledger, newest-first transaction order, and adjacent
balance changes. It accepts both `Balance (CAD)` and the observed currency-only
`(CAD)` ledger header. The newest transaction's reported balance is authoritative.
New exports must provide continuous activity from the tracked balance; unrelated
older rows added by widening the date range are not replayed as new movements.

If no matching CSV exists, use the encrypted cached balance with its original
dates. An unchanged export does not advance those dates. An export older than
the cached source is ignored. Missing initial balances, malformed tracked exports,
and discontinuous new activity are visible errors rather than zero balances.

The loading status shows the CSV outcome and last activity date once. There is
no elapsed-age cutoff that turns an unchanged balance into zero or blocks it.
Scheduled runs use saved Saven data without scanning Downloads.

To inspect or save an export separately:

```powershell
python scripts/import_saven_balance.py --directory "$HOME/Downloads"
python scripts/import_saven_balance.py --directory "$HOME/Downloads" --save
```

`--save` updates transfer evidence together with the balance snapshot. Inspection
without `--save` changes neither. Seed the snapshot locally before publishing
Saven targets to scheduled runs.

## Recognizing transfers

Transfers follow the configured Margin account ↔ linked bank ↔ Saven route. An
outstanding recommendation supplies intent; newly observed cash movements on
that route supply evidence. This is an inference, not an end-to-end bank transfer
identifier. The account-activity API exposes posted movements, not an unposted
withdrawal request.

Recognized source principal is capped across the whole recommendation:

`min(cumulative related source withdrawals, original recommended amount)`

An unexecuted recommendation is not an asset. Amounts above the cap are ordinary
outflows from the tracked portfolio; they do not cause warnings or pause history,
tactical evaluation, accuracy reporting, or planning. Actual destination balances
are always uncapped. For a $180,000 recommendation:

| Source withdrawal and destination receipt | Treatment |
| --- | --- |
| Withdraw $165,000; nothing received yet | $165,000 in transit. The unused $15,000 is not money. |
| Withdraw $200,000; nothing received yet | At most $180,000 in transit; the other $20,000 leaves the tracked portfolio. |
| Withdraw $165,000; receive $180,000 with a bank top-up | Clear $165,000 of transit and include the full actual receipt. The extra $15,000 joins the portfolio once. |
| Withdraw $200,000; receive $180,000 and spend $20,000 elsewhere | The tracked portfolio is $20,000 lower. |
| Withdraw $200,000; actually receive all $200,000 | Use the full actual destination balance. The transit cap does not cap real account value. |

### Questrade to Saven

Posted CAD withdrawals from the linked account establish transit, up to the
original cap. The recognized amount is an external household asset, separate from
broker buying power. As a new CSV confirms receipts, replace matched transit with
the reported Saven balance without double-counting. A receipt can include money
already at the linked bank; an exact source/destination amount match is not required.

### Saven to Questrade

Process a new CSV as soon as it shows the withdrawal; waiting for Questrade is not
required. The actual lower Saven balance plus recognized transit preserves the
tracked value until the broker receives the money.

If Questrade receives first, an `Awaiting CSV` deduction offsets the stale cached
Saven valuation. A CSV confirming the source withdrawal reconciles that estimate.
An interest-only export does not establish a withdrawal. Interest and service
fees affect the balance but do not count as transfer principal.

A withdrawal confirmed by the current CSV is authoritative. A larger broker
receipt is treated as including outside-bank funds. If another Saven withdrawal
occurs after that export, a newer CSV is needed to identify its source. Without
fresh Saven data, those two possibilities cannot be distinguished reliably.

## Recommendation lifecycle and state

The original cap survives repeated reports and partial payments. Unused intent is
dropped if the local plan drops it, or replaced if its direction changes. Once
money has moved, a partly settled cycle retains its remaining allowance even if
the remaining drift falls below tolerance. This is internal bookkeeping, not an
asset or a reason to leave a settled transfer row on screen.

A cycle closes when receipts reach its original recommendation, or the capped
source amount has been fully sent and received. A settled partial cycle can also
be superseded by an opposite-direction recommendation. A route with unsettled
money cannot be removed or changed. There is no timer or expiry window. The API's
31-day request limit is a query constraint, not a display-retention period.

Both state files are app-managed SOPS ciphertext in the private repo:

| File | Contents |
| --- | --- |
| `data/saven_balance.json` | Account mapping with exactly `balance_cad`, `last_transaction_date`, `source_modified_at`, and `source_sha256`. |
| `data/transfer_tracking.json` | Recommendation context/cap, cumulative integer-cent amounts, and observation signatures needed to avoid replay. |

Signatures retain multiplicity: two otherwise identical withdrawals remain two
movements, while repeated imports and overlapping API windows do not count them
again. Keep real identifiers, exports, and live state outside the public repo.

## Valuation, planning, and display

Reconciliation runs before history recording and tactical evaluation in local
and scheduled runs. Source-read failures do not produce a history/tactical update
from an unreconciled snapshot. Routine transfers and excess outflows do not pause
those calculations.

Portfolio Holdings groups Saven and transfer adjustments below brokerage cash.
Account Summary uses the same household scope, so both totals reconcile. Cash
and position cells for external/adjustment rows are blank. Reported balances stay
visible, with transit assets or negative CSV adjustments on separate rows.

Saven-bound transit counts toward its destination allocation, avoiding artificial
drift while that cash is traveling. Broker-bound transit remains uninvested until
its projected arrival and buys. Projected buys can use that expected arrival as
they use planned conversions; actual broker cash is unchanged until it posts.

Saven Transfers follows currency conversions and shows direction, amount, and
`Planned`, `In transit`, or `Awaiting CSV`. Settled rows are omitted immediately.
When nothing remains to recommend or reconcile, the entire box disappears.
Raw symbols render as `SAVEN:...<last-four>`; matching uses full identifiers.

## First real-transfer check

Synthetic scenario tests cover caps, partial movements, top-ups, both arrival
orders, replay, and planner funding. The next real transfer still needs validation:

1. Confirm the local recommendation and observation baseline have synced before
   moving money.
2. After a posted movement, compare the actual account balances, transit/CSV
   adjustment, and both household totals. Check that activity is not replayed.
3. Import a continuous CSV when Saven records the movement, including a withdrawal
   before the broker receives it. Verify the eventual arrival clears transit or
   the stale-source adjustment without duplicating value.
4. Check that the box disappears when settled, the recommendation cap has not
   reopened, and history/tactical values reflect the tracked portfolio.

Pay particular attention to provider posting delays and same-day ordering:
activity dates can be date-only. For implementation details, see the
[architecture](architecture.md#transfer-state-boundary),
[planner reconciliation](rebalancer.md#automatic-transit-reconciliation), and
[scenario tests](../tests/test_transfer_tracking.py).

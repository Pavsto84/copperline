# Bank reconciliation

Copperline separates imported bank observations from accounting journals.

An imported transaction does not post to the ledger by itself.

## Candidate scoring

Candidates are limited to entries affecting the selected bank ledger account within a date window.

Score weights:

- amount: 65%
- date proximity: 20%
- description/reference similarity: 15%

Exact amount receives the highest amount score.

## Automatic matching

Auto-match requires:

- top candidate above threshold
- enough margin above the runner-up
- no existing bank transaction matched to that journal entry
- exact bank/ledger amount unless explicitly overridden

The conservative policy is intentional.

## Statement session

A reconciliation session stores statement date and ending balance.

Completion verifies:

```text
ledger bank balance - statement ending balance
```

is within configured minor-unit tolerance.

By default, every imported bank transaction through the statement date must also be matched or ignored.

## Unmatching

A matched transaction can be returned to unmatched state without reversing the accounting entry. Reconciliation links are operational metadata, not ledger postings.

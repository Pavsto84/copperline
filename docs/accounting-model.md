# Accounting model

## Debit and credit

Journal lines store independent non-negative debit and credit amounts. A line may use one side only.

For each journal:

```text
sum(debits) == sum(credits)
```

Copperline validates this before a draft is persisted and again before posting.

## Normal balances

Assets and expenses have debit normal balances.

Liabilities, equity, and revenue have credit normal balances.

`LedgerService.balance()` returns balances using normal-balance sign, while `raw_debit_credit()` returns literal journal sides.

## Posted entries

Posted entries are not edited in place. Corrections use reversal entries.

Draft entries can be voided because they have not entered accounting history.

## Period locks

A defined accounting period can be locked after drafts are cleared. A locked period rejects new journal posting.

Unlocking requires an explicit reason and records an audit event.

## Invoices

Sales invoice:

```text
Dr Accounts Receivable       gross
    Cr Revenue               net
    Cr Tax Payable           tax
```

Purchase invoice:

```text
Dr Expense/Asset             net + nonrecoverable tax
Dr Recoverable Tax           recoverable tax
    Cr Accounts Payable      gross
```

A credit note uses the opposite sides rather than negative debit/credit fields.

## Payments

Customer receipt:

```text
Dr Bank
    Cr Accounts Receivable
```

Vendor payment:

```text
Dr Accounts Payable
    Cr Bank
```

Allocation changes invoice settlement status but does not create another journal entry.

## Inventory

Receipt:

```text
Dr Inventory
    Cr Accounts Payable / clearing
```

Issue:

```text
Dr Cost of Goods Sold
    Cr Inventory
```

FIFO or moving-average cost layers determine the issue value.

Transfers between warehouses do not change total ledger value and therefore do not post an accounting entry.

## Current earnings

The balance-sheet report includes current-period cumulative revenue less expense as current earnings because closing entries into retained earnings are not automatically generated in the baseline release.

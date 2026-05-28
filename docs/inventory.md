# Inventory

Copperline tracks quantity and accounting value together.

## Items

Inventory items reference:

- sales income account
- expense/COGS account
- inventory asset account
- currency
- costing method

Service and non-inventory items do not create stock movements.

## FIFO

Each receipt creates a cost layer. Issues consume oldest available layers by acquisition date and id.

The movement stores a weighted unit cost for display, while `inventory_consumptions` records exact source layers.

## Moving average

Moving-average items value an issue at:

```text
current inventory value / current quantity
```

Underlying layers are proportionally reduced so the subledger continues to tie to the remaining valuation.

## Transfers

A transfer consumes source cost layers and creates one destination layer at the weighted transferred cost.

No journal is posted because company-wide inventory value is unchanged.

## Adjustments

Positive count adjustments behave like receipts. Negative adjustments behave like issues.

A reason is required. Operators may choose an explicit adjustment offset account.

## Invariants

For an inventory item and warehouse:

```text
quantity on hand = sum(inventory movements)
```

For open cost layers:

```text
inventory valuation =
sum(remaining quantity * unit cost)
```

Operational procedures should periodically compare the inventory valuation report with the inventory asset general-ledger account.

## Release review focus

For release validation, cover both FIFO and moving-average items with receipts, issues, transfers, and adjustments. Reconcile the inventory valuation report to the configured inventory asset account after each stock movement scenario.

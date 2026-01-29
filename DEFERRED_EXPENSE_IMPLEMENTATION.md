# Documentation: Deferred Expense Handling for Delivery Notes

## 📌 Overview

This document serves as a comprehensive technical and functional reference for the implementation of **Deferred Expense Accounting** within the `Delivery Note` doctype in ERPNext, handled via the `custom_temp` app.

The primary goal was to replicate the behavior of **Purchase Invoice Deferred Expenses** but for **Delivery Notes**, specifically in environments where **Perpetual Inventory is disabled**.

---

## 🚀 1. The Challenge (Problem Statement)

### Standard ERPNext Behavior

- **Perpetual Inventory OFF**: Delivery Notes usually don't create GL Entries. They only update stock levels.
- **Deferred Expenses**: Only natively supported in Purchase Invoices (for expenses) and Sales Invoices (for revenue).
- **The Gap**: Users selling services or items that require expense recognition over time (amortization) through a Delivery Note had no automated way to move costs from a "Deferred Expense" (Asset) account to a "COGS" (Expense) account over a period.

### Requirements

1. Force GL Entries on Delivery Note submission even if Perpetual Inventory is OFF.
2. Link specific items to a **Deferred Expense Account** and a **Service Period**.
3. Automatically recognize (amortize) the expense monthly via a scheduled job.
4. Use direct **GL Entries** (instead of Journal Entries) for a cleaner Audit Trail.

---

## 🛠 2. Technical Solution Architecture

The solution is built on four pillars: **Overrides**, **Client Scripts**, **Scheduled Tasks**, and **Custom Fields**.

### A. Custom Fields (Delivery Note Item)

We added custom fields to `tabDelivery Note Item` through Fixtures:

- `enable_deferred_expense`: Checkbox to trigger logic.
- `deferred_expense_account`: The asset account (e.g., _Prepaid Expenses_).
- `service_start_date` / `service_end_date`: Defines the amortization period.

### B. Controller Overrides (`overrides/delivery_note.py`)

We extended the standard `DeliveryNote` class to hijack the GL entry creation process.

**Key Logic:**

- **`make_gl_entries`**: Forces execution even when inventory settings would otherwise skip it.
- **`get_gl_entries`**:
  - Debits the **Deferred Expense Account** (Asset).
  - Credits the **Warehouse Account** (or Stock Asset).
  - Passes `voucher_detail_no` (Item Row Name) to every GL Entry. This is critical for tracking how much has been "amortized" per specific item row.

### C. Client-Side Automation (`public/js/delivery_note.js`)

To ensure a smooth UX, we implemented:

- **Auto-fetching**: When an item is selected, we fetch deferred settings from the `Item` master using a whitelisted API.
- **Date Calculation**: Automatically sets the service start date to the posting date and calculates the end date based on the item's "No of Months" setting.

### D. The Amortization Engine (`tasks.py`)

This is the core "Pro" logic that runs in the background.

**The Workflow:**

1. **Selection**: Finds all submitted DN items with `enable_deferred_expense=1` where the service period is active.
2. **Calculation**:
   - Checks `Accounts Settings` to see if amortization is "Monthly" or "Daily". (However, here we force Monthly)
   - Calculates the amount for the current month.
   - **Catch-up**: If a month was missed, the recursive logic processes all pending months until it hits the current date.
3. **Execution**:
   - Creates a balanced GL Entry pair:
     - **Debit**: Expense Account (COGS).
     - **Credit**: Deferred Expense Account (Asset).
4. **Stop Conditions**:
   - Stops if the `Remaining Amount` reaches 0.
   - Stops if the `Service End Date` is passed.

---

## 📖 3. Functional Guide (User Manual)

### Step 1: Item Setup

1. In the **Item Master**, enable "Deferred Expense".
2. Set the "No of Months" for recognition.
3. Define the default "Deferred Expense Account" in the **Item Defaults** table.

### Step 2: Delivery Note Creation

1. Create a Delivery Note.
2. When adding items, the "Enable Deferred Expense" checkbox and accounts are auto-filled.
3. Adjust the **Service Start** and **End** dates if necessary.
4. **Submit**. On submission, the system creates an initial GL entry:
   - _Debit: Deferred Expense Account_
   - _Credit: Stock Asset Account_

### Step 3: Automatic Recognition

- On the last day of every month (or via daily catch-up), the system automatically runs the background job.
- It calculates the portion for that month and creates a GL Entry moving that amount from the Asset account to the Expense account.

---

## 👨‍💻 4. Pro-Tips for Future Implementations

### 1. The Power of `voucher_detail_no`

Always link your background GL entries to the specific item row using `voucher_detail_no`. This allows you to run SQL queries to calculate exactly how much has been amortized for _that specific row_ without getting confused by other rows in the same document.

### 2. Recursive Processing

When building schedulers, always assume they might fail or be skipped (e.g., server downtime). By using **Recursive Catch-up** logic (processing one month and then calling itself again if more time remains), you ensure that your accounting books are always eventually correct.

### 3. GL Entry vs. Journal Entry

- **Journal Entries** are easier to see in the UI list, but they create a "middleman" document.
- **Direct GL Entries** (using `make_gl_entries`) are faster, safer, and are what ERPNext uses internally for standard transactions. For professional "Engine-level" features, prefer direct GL Entries.

### 4. Naming Series Diagnostics

If your manual GL entries are getting hash names (random characters), it's usually because the `Series` record in the database is missing for your naming pattern (e.g., `ACC-GLE-.2026.-`). Use a small script to verify and insert the series record in `tabSeries`.

---

## 📂 5. File Reference

- `overrides/delivery_note.py`: The Controller Override.
- `tasks.py`: The Amortization Logic.
- `api.py`: Backend-to-Frontend communication.
- `public/js/delivery_note.js`: UI Automation.
- `fix_gl_naming.py`: Maintenance utility.

---

_Created by Antigravity AI for the custom_temp app._

"""
Deferred Expense Processing for Delivery Notes

This module implements deferred expense accounting for Delivery Notes,
similar to how ERPNext handles deferred accounting for Purchase Invoices.

The flow is:
1. When a Delivery Note is submitted with deferred expense enabled items,
   the GL entry posts to the Deferred Expense Account instead of COGS
2. The scheduler runs monthly to recognize the deferred expense
3. Journal Entries are created to move amounts from Deferred Expense to COGS
"""

import frappe
from frappe import _
from frappe.utils import (
    add_days,
    add_months,
    cint,
    date_diff,
    flt,
    get_first_day,
    get_last_day,
    getdate,
    rounded,
    today,
)
from erpnext.accounts.utils import get_account_currency
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
    get_accounting_dimensions,
)


def process_delivery_note_deferred_expense(posting_date=None):
    """
    Main entry point for processing deferred expense for all eligible Delivery Notes.
    Similar to ERPNext's process_deferred_accounting function.

    This function:
    1. Finds all Delivery Note items with deferred expense enabled
    2. Filters items whose service period overlaps with the processing period
    3. Creates Journal Entries to recognize the expense

    Args:
        posting_date: The date up to which to process. Defaults to today.

    Returns:
        int: Number of Journal Entries created
    """
    if not posting_date:
        posting_date = today()

    # Process for the last month up to yesterday (or specified posting_date)
    # This matches ERPNext's behavior
    start_date = add_months(getdate(posting_date), -1)
    end_date = (
        add_days(getdate(posting_date), -1)
        if posting_date == today()
        else getdate(posting_date)
    )

    # Find all Delivery Note items that need processing
    delivery_note_items = frappe.db.sql(
        """
        SELECT item.name as item_name, item.parent
        FROM `tabDelivery Note Item` item
        INNER JOIN `tabDelivery Note` dn ON item.parent = dn.name
        WHERE item.custom_enable_deferred_expense = 1
        AND item.custom_service_start_date <= %s
        AND item.custom_service_end_date >= %s
        AND dn.docstatus = 1
        AND IFNULL(item.custom_deferred_expense_account, '') != ''
        """,
        (end_date, start_date),
        as_dict=True,
    )

    count = 0
    processed_dns = set()

    for row in delivery_note_items:
        try:
            doc = frappe.get_doc("Delivery Note", row.parent)

            # Find the specific item
            item = None
            for i in doc.items:
                if i.name == row.item_name:
                    item = i
                    break

            if not item:
                continue

            # Book deferred expense for this item
            entries_made = book_deferred_expense_recursive(doc, item, posting_date)
            count += entries_made

            if entries_made > 0:
                processed_dns.add(doc.name)

        except Exception as e:
            frappe.log_error(
                message=f"Error processing deferred expense for DN {row.parent}, Item {row.item_name}: {str(e)}",
                title="Deferred Expense Processing Error",
            )
            continue

    if count > 0:
        frappe.db.commit()
        frappe.logger().info(
            f"Processed deferred expense for {len(processed_dns)} Delivery Notes, created {count} Journal Entries"
        )

    return count


def book_deferred_expense_recursive(doc, item, posting_date):
    """
    Recursively book deferred expense for an item.
    This handles cases where multiple months need to be caught up.

    Args:
        doc: The Delivery Note document
        item: The Delivery Note Item row
        posting_date: The date up to which to process

    Returns:
        int: Number of entries created
    """
    start_date, end_date, last_gl_entry = get_booking_dates(doc, item, posting_date)

    if not (start_date and end_date):
        return 0

    # Fetch Accounts
    deferred_account = item.custom_deferred_expense_account
    expense_account = get_expense_account(item, doc.company)

    if not (deferred_account and expense_account):
        frappe.log_error(
            message=f"Missing accounts for DN {doc.name}, Item {item.item_code}. "
            f"Deferred: {deferred_account}, Expense: {expense_account}",
            title="Deferred Expense Account Missing",
        )
        return 0

    account_currency = get_account_currency(expense_account)

    total_days = (
        date_diff(item.custom_service_end_date, item.custom_service_start_date) + 1
    )
    total_booking_days = date_diff(end_date, start_date) + 1

    # Get booking method from Accounts Settings (Months or Days)
    book_deferred_entries_based_on = (
        frappe.db.get_single_value(
            "Accounts Settings", "book_deferred_entries_based_on"
        )
        or "Months"
    )

    # Calculate Amount
    if book_deferred_entries_based_on == "Months":
        amount, base_amount = calculate_monthly_amount(
            doc,
            item,
            last_gl_entry,
            start_date,
            end_date,
            total_days,
            total_booking_days,
            account_currency,
        )
    else:
        amount, base_amount = calculate_daily_amount(
            doc, item, last_gl_entry, total_days, total_booking_days, account_currency
        )

    entries_made = 0

    if flt(amount) > 0:
        # Check if books are frozen
        accounts_frozen_upto = frappe.db.get_single_value(
            "Accounts Settings", "acc_frozen_upto"
        )
        gl_posting_date = end_date

        if accounts_frozen_upto and getdate(end_date) <= getdate(accounts_frozen_upto):
            gl_posting_date = get_last_day(add_days(accounts_frozen_upto, 1))

        # Create Journal Entry
        entries_made = create_deferred_expense_journal_entry(
            doc,
            item,
            expense_account,
            deferred_account,
            amount,
            base_amount,
            gl_posting_date,
            account_currency,
        )

    # Recursive call to catch up if needed
    if getdate(end_date) < getdate(posting_date) and not last_gl_entry:
        entries_made += book_deferred_expense_recursive(doc, item, posting_date)

    return entries_made


def get_expense_account(item, company):
    """Get the expense account for the item."""
    expense_account = item.expense_account

    if not expense_account:
        expense_account = frappe.db.get_value(
            "Item Default",
            {"parent": item.item_code, "company": company},
            "expense_account",
        )

    if not expense_account:
        expense_account = frappe.db.get_value(
            "Company", company, "default_expense_account"
        )

    return expense_account


def get_booking_dates(doc, item, posting_date=None):
    """
    Determine the start and end dates for booking deferred expense.
    This checks for previous bookings and calculates the next period.

    Args:
        doc: The Delivery Note document
        item: The Delivery Note Item row
        posting_date: The date up to which to process

    Returns:
        tuple: (start_date, end_date, last_gl_entry)
    """
    if not posting_date:
        posting_date = add_days(today(), -1)

    last_gl_entry = False
    deferred_account = item.custom_deferred_expense_account

    # Find the last Journal Entry booking for this specific item
    prev_gl_via_je = frappe.db.sql(
        """
        SELECT p.name, p.posting_date
        FROM `tabJournal Entry` p
        INNER JOIN `tabJournal Entry Account` c ON p.name = c.parent
        WHERE p.company = %s
        AND c.account = %s
        AND c.reference_type = %s
        AND c.reference_name = %s
        AND c.reference_detail_no = %s
        AND p.docstatus < 2
        ORDER BY p.posting_date DESC
        LIMIT 1
        """,
        (doc.company, deferred_account, doc.doctype, doc.name, item.name),
        as_dict=True,
    )

    if prev_gl_via_je:
        start_date = getdate(add_days(prev_gl_via_je[0].posting_date, 1))
    else:
        start_date = getdate(item.custom_service_start_date)

    end_date = get_last_day(start_date)

    # Cap end_date at service end date
    service_end = getdate(item.custom_service_end_date)
    if end_date >= service_end:
        end_date = service_end
        last_gl_entry = True

    # Check for service stop date (if implemented)
    service_stop = item.get("custom_service_stop_date")
    if service_stop and end_date >= getdate(service_stop):
        end_date = getdate(service_stop)
        last_gl_entry = True

    # Cap at posting date (don't book future)
    if end_date > getdate(posting_date):
        end_date = getdate(posting_date)

    if getdate(start_date) <= getdate(end_date):
        return start_date, end_date, last_gl_entry
    else:
        return None, None, None


def get_already_booked_amount(doc, item):
    """
    Get the amount already booked for this item via previous Journal Entries.

    Args:
        doc: The Delivery Note document
        item: The Delivery Note Item row

    Returns:
        tuple: (already_booked_amount, already_booked_amount_in_account_currency)
    """
    deferred_account = item.custom_deferred_expense_account

    # Sum credits to deferred account (amortization reduces the deferred asset)
    journal_entry_details = frappe.db.sql(
        """
        SELECT
            SUM(c.credit) as total_credit,
            SUM(c.credit_in_account_currency) as total_credit_in_account_currency
        FROM `tabJournal Entry` p
        INNER JOIN `tabJournal Entry Account` c ON p.name = c.parent
        WHERE p.company = %s
        AND c.account = %s
        AND c.reference_type = %s
        AND c.reference_name = %s
        AND c.reference_detail_no = %s
        AND p.docstatus < 2
        """,
        (doc.company, deferred_account, doc.doctype, doc.name, item.name),
        as_dict=True,
    )

    already_booked_amount = flt(
        journal_entry_details[0].total_credit
        if journal_entry_details and journal_entry_details[0].total_credit
        else 0.0
    )

    if doc.currency == doc.company_currency:
        already_booked_amount_in_account_currency = already_booked_amount
    else:
        already_booked_amount_in_account_currency = flt(
            journal_entry_details[0].total_credit_in_account_currency
            if journal_entry_details
            and journal_entry_details[0].total_credit_in_account_currency
            else 0.0
        )

    return already_booked_amount, already_booked_amount_in_account_currency


def calculate_monthly_amount(
    doc,
    item,
    last_gl_entry,
    start_date,
    end_date,
    total_days,
    total_booking_days,
    account_currency,
):
    """
    Calculate the amount to book based on monthly proration.
    This follows ERPNext's calculate_monthly_amount logic.
    """
    amount, base_amount = 0, 0

    # Get the total amounts
    total_base_amount_val = flt(
        item.get("base_net_amount") or item.get("base_amount") or item.amount
    )
    total_amount_val = flt(item.get("net_amount") or item.amount)

    if not last_gl_entry:
        # Calculate total months in service period
        service_start = getdate(item.custom_service_start_date)
        service_end = getdate(item.custom_service_end_date)

        total_months = (
            (service_end.year - service_start.year) * 12
            + (service_end.month - service_start.month)
            + 1
        )

        # Calculate prorate factor
        denom = flt(
            date_diff(get_last_day(service_end), get_first_day(service_start)) + 1
        )
        if denom == 0:
            denom = 1

        prorate_factor = flt(date_diff(service_end, service_start) + 1) / denom
        actual_months = rounded(total_months * prorate_factor, 1)

        if actual_months == 0:
            actual_months = 1

        already_booked, already_booked_ac = get_already_booked_amount(doc, item)

        base_amount = flt(
            total_base_amount_val / actual_months,
            item.precision("amount") if hasattr(item, "precision") else 2,
        )

        if base_amount + already_booked > total_base_amount_val:
            base_amount = total_base_amount_val - already_booked

        if account_currency == doc.company_currency:
            amount = base_amount
        else:
            amount = flt(
                total_amount_val / actual_months,
                item.precision("amount") if hasattr(item, "precision") else 2,
            )
            if amount + already_booked_ac > total_amount_val:
                amount = total_amount_val - already_booked_ac

        # Partial month logic
        if not (
            get_first_day(start_date) == start_date
            and get_last_day(end_date) == end_date
        ):
            day_denom = flt(
                date_diff(get_last_day(end_date), get_first_day(start_date)) + 1
            )
            if day_denom == 0:
                day_denom = 1
            partial_month = flt(date_diff(end_date, start_date) + 1) / day_denom

            base_amount = rounded(partial_month * base_amount, 2)
            amount = rounded(partial_month * amount, 2)

    else:
        # Last entry - book remaining amount
        already_booked, already_booked_ac = get_already_booked_amount(doc, item)
        base_amount = flt(
            total_base_amount_val - already_booked,
            item.precision("amount") if hasattr(item, "precision") else 2,
        )

        if account_currency == doc.company_currency:
            amount = base_amount
        else:
            amount = flt(
                total_amount_val - already_booked_ac,
                item.precision("amount") if hasattr(item, "precision") else 2,
            )

    return amount, base_amount


def calculate_daily_amount(
    doc, item, last_gl_entry, total_days, total_booking_days, account_currency
):
    """
    Calculate the amount to book based on daily proration.
    This follows ERPNext's calculate_amount logic.
    """
    amount, base_amount = 0, 0

    total_base_amount_val = flt(
        item.get("base_net_amount") or item.get("base_amount") or item.amount
    )
    total_amount_val = flt(item.get("net_amount") or item.amount)

    if not last_gl_entry:
        base_amount = flt(
            total_base_amount_val * total_booking_days / flt(total_days),
            item.precision("amount") if hasattr(item, "precision") else 2,
        )
        if account_currency == doc.company_currency:
            amount = base_amount
        else:
            amount = flt(
                total_amount_val * total_booking_days / flt(total_days),
                item.precision("amount") if hasattr(item, "precision") else 2,
            )
    else:
        # Last entry - book remaining amount
        already_booked, already_booked_ac = get_already_booked_amount(doc, item)
        base_amount = flt(
            total_base_amount_val - already_booked,
            item.precision("amount") if hasattr(item, "precision") else 2,
        )
        if account_currency == doc.company_currency:
            amount = base_amount
        else:
            amount = flt(
                total_amount_val - already_booked_ac,
                item.precision("amount") if hasattr(item, "precision") else 2,
            )

    return amount, base_amount


def create_deferred_expense_journal_entry(
    doc,
    item,
    expense_account,
    deferred_account,
    amount,
    base_amount,
    posting_date,
    account_currency,
):
    """
    Create a Journal Entry to recognize deferred expense.

    The entry:
    - Debits the Expense Account (recognize expense)
    - Credits the Deferred Expense Account (reduce asset)
    """
    if amount == 0:
        return 0

    # Check if we should auto-submit based on Accounts Settings
    submit_journal_entry = cint(
        frappe.db.get_single_value("Accounts Settings", "submit_journal_entries")
    )

    je = frappe.new_doc("Journal Entry")
    je.posting_date = posting_date
    je.company = doc.company
    je.voucher_type = "Deferred Expense"
    je.user_remark = f"Deferred Expense Recognition for Delivery Note {doc.name}, Item {item.item_code}"
    je.cheque_no = doc.name
    je.cheque_date = posting_date

    # Debit Expense Account (Recognize Expense)
    debit_entry = {
        "account": expense_account,
        "debit_in_account_currency": amount,
        "credit_in_account_currency": 0,
        "account_currency": account_currency,
        "reference_type": doc.doctype,
        "reference_name": doc.name,
        "reference_detail_no": item.name,
        "cost_center": item.cost_center,
        "project": item.get("project"),
    }

    # Credit Deferred Expense Account (Reduce Asset)
    credit_entry = {
        "account": deferred_account,
        "debit_in_account_currency": 0,
        "credit_in_account_currency": amount,
        "account_currency": account_currency,
        "reference_type": doc.doctype,
        "reference_name": doc.name,
        "reference_detail_no": item.name,
        "cost_center": item.cost_center,
        "project": item.get("project"),
    }

    # Add accounting dimensions
    for dimension in get_accounting_dimensions():
        debit_entry[dimension] = item.get(dimension)
        credit_entry[dimension] = item.get(dimension)

    je.append("accounts", debit_entry)
    je.append("accounts", credit_entry)

    try:
        je.save(ignore_permissions=True)

        if submit_journal_entry:
            je.submit()

        frappe.logger().info(
            f"Created Deferred Expense JE {je.name} for DN {doc.name}, Item {item.item_code}, Amount {amount}"
        )
        return 1

    except Exception as e:
        frappe.log_error(
            message=f"Error creating Deferred Expense JE for DN {doc.name}, Item {item.item_code}: {str(e)}",
            title="Deferred Expense JE Creation Error",
        )
        frappe.db.rollback()
        return 0

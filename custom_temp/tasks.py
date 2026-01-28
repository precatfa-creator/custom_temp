"""
Deferred Expense Processing for Delivery Notes

This module implements deferred expense accounting for Delivery Notes using GL Entries.
The monthly scheduled job:
1. Finds all submitted DN items with deferred expense enabled
2. Calculates monthly amortization amounts
3. Creates GL Entries to transfer from Deferred Expense Account to COGS
4. Stops when remaining amount = 0 or service_end_date is reached

Key Features:
- Uses direct GL Entries (not Journal Entries) for better performance
- Tracks progress via voucher_detail_no to prevent double-booking
- Supports both monthly and daily booking methods
- Handles partial months and catch-up scenarios
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
from erpnext.accounts.general_ledger import make_gl_entries
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
    get_accounting_dimensions,
)


# =============================================================================
# MAIN SCHEDULED JOB
# =============================================================================


def process_delivery_note_deferred_expenses():
    """
    Main scheduled job entry point for processing deferred expenses.
    This is called monthly by the scheduler.

    Returns:
        int: Number of GL Entry pairs created
    """
    posting_date = today()

    frappe.logger().info(
        f"[DN Deferred Expense] Starting processing for date: {posting_date}"
    )

    # Get all eligible DN items
    eligible_items = _get_eligible_deferred_items(posting_date)

    if not eligible_items:
        frappe.logger().info("[DN Deferred Expense] No eligible items found")
        return 0

    total_entries = 0
    processed_dns = set()
    failed_items = []

    for row in eligible_items:
        try:
            entries_made = _process_single_item(
                dn_name=row.dn_name,
                item_name=row.item_name,
                posting_date=posting_date,
            )

            total_entries += entries_made

            if entries_made > 0:
                processed_dns.add(row.dn_name)

        except Exception as e:
            failed_items.append(
                {"dn": row.dn_name, "item": row.item_name, "error": str(e)}
            )
            frappe.log_error(
                message=f"Error processing DN {row.dn_name}, Item {row.item_name}: {str(e)}",
                title="DN Deferred Expense Processing Error",
            )
            continue

    # Commit all changes
    if total_entries > 0:
        frappe.db.commit()

    # Log summary
    frappe.logger().info(
        f"[DN Deferred Expense] Completed. "
        f"Processed {len(processed_dns)} Delivery Notes, "
        f"Created {total_entries} GL Entry pairs, "
        f"Failed items: {len(failed_items)}"
    )

    # Log failures if any
    if failed_items:
        frappe.log_error(
            message=f"Failed items:\n{frappe.as_json(failed_items)}",
            title="DN Deferred Expense - Failed Items",
        )

    return total_entries


# =============================================================================
# QUERY FUNCTIONS
# =============================================================================


def _get_eligible_deferred_items(posting_date):
    """
    Find all Delivery Note items eligible for deferred expense processing.

    Criteria:
    1. Deferred expense is enabled
    2. Service start date <= today
    3. Service end date >= first day of previous month (for catch-up)
    4. DN is submitted (docstatus = 1)
    5. Has valid deferred expense account
    6. Has remaining amount to book

    Args:
        posting_date: The date to process up to

    Returns:
        list: List of eligible items with dn_name and item_name
    """
    start_of_period = get_first_day(add_months(getdate(posting_date), -1))

    eligible_items = frappe.db.sql(
        """
        SELECT
            item.name AS item_name,
            item.parent AS dn_name,
            item.item_code,
            item.custom_service_start_date,
            item.custom_service_end_date,
            item.custom_deferred_expense_account,
            IFNULL(item.base_net_amount, item.amount) AS total_amount
        FROM `tabDelivery Note Item` item
        INNER JOIN `tabDelivery Note` dn ON item.parent = dn.name
        WHERE item.custom_enable_deferred_expense = 1
            AND item.custom_service_start_date <= %(posting_date)s
            AND item.custom_service_end_date >= %(start_of_period)s
            AND dn.docstatus = 1
            AND IFNULL(item.custom_deferred_expense_account, '') != ''
            AND IFNULL(item.amount, 0) > 0
        ORDER BY dn.posting_date ASC, item.idx ASC
        """,
        {"posting_date": posting_date, "start_of_period": start_of_period},
        as_dict=True,
    )

    return eligible_items


def _get_already_booked_amount(company, deferred_account, dn_name, item_name):
    """
    Get the total amount already booked (credited) for this item.

    We track by:
    1. GL Entries with voucher_detail_no = item.name (from catch-up GL postings)
    2. GL Entries created for amortization with against_voucher_type/no

    For deferred expense, amortization CREDITS the deferred account.

    Args:
        company: Company name
        deferred_account: The deferred expense account
        dn_name: Delivery Note name
        item_name: Delivery Note Item name (row ID)

    Returns:
        float: Total amount already credited (booked)
    """
    # Check GL Entries directly linked to this item
    gl_amount = frappe.db.sql(
        """
        SELECT COALESCE(SUM(credit), 0) as total_credit
        FROM `tabGL Entry`
        WHERE company = %(company)s
            AND account = %(account)s
            AND voucher_type = 'Delivery Note'
            AND voucher_no = %(voucher_no)s
            AND is_cancelled = 0
            AND (
                voucher_detail_no = %(item_name)s
                OR against_voucher = %(voucher_no)s
            )
        """,
        {
            "company": company,
            "account": deferred_account,
            "voucher_no": dn_name,
            "item_name": item_name,
        },
        as_dict=True,
    )

    return flt(gl_amount[0].total_credit if gl_amount else 0)


def _get_last_booking_date(company, deferred_account, dn_name, item_name):
    """
    Get the last posting date when this item was booked.

    Args:
        company: Company name
        deferred_account: The deferred expense account
        dn_name: Delivery Note name
        item_name: Delivery Note Item name

    Returns:
        date or None: Last booking date or None if never booked
    """
    last_entry = frappe.db.sql(
        """
        SELECT MAX(posting_date) as last_date
        FROM `tabGL Entry`
        WHERE company = %(company)s
            AND account = %(account)s
            AND voucher_type = 'Delivery Note'
            AND voucher_no = %(voucher_no)s
            AND is_cancelled = 0
            AND credit > 0
        """,
        {
            "company": company,
            "account": deferred_account,
            "voucher_no": dn_name,
        },
        as_dict=True,
    )

    return (
        getdate(last_entry[0].last_date)
        if last_entry and last_entry[0].last_date
        else None
    )


# =============================================================================
# PROCESSING FUNCTIONS
# =============================================================================


def _process_single_item(dn_name, item_name, posting_date):
    """
    Process deferred expense for a single DN item.
    Handles recursive catch-up if multiple months need processing.

    Args:
        dn_name: Delivery Note name
        item_name: Delivery Note Item row name
        posting_date: Date to process up to

    Returns:
        int: Number of GL entry pairs created
    """
    doc = frappe.get_doc("Delivery Note", dn_name)

    # Find the specific item
    item = None
    for i in doc.items:
        if i.name == item_name:
            item = i
            break

    if not item:
        return 0

    # Validate required fields
    if not item.get("custom_enable_deferred_expense"):
        return 0

    if not item.get("custom_deferred_expense_account"):
        return 0

    # Get accounts
    deferred_account = item.custom_deferred_expense_account
    expense_account = _get_expense_account(item, doc.company)

    if not expense_account:
        frappe.log_error(
            message=f"No expense account found for DN {dn_name}, Item {item.item_code}",
            title="DN Deferred Expense - Missing Account",
        )
        return 0

    # Process with recursive catch-up
    return _book_deferred_expense(
        doc=doc,
        item=item,
        deferred_account=deferred_account,
        expense_account=expense_account,
        posting_date=posting_date,
    )


def _book_deferred_expense(
    doc, item, deferred_account, expense_account, posting_date, prev_end_date=None
):
    """
    Book deferred expense for an item. Recursively handles multiple months.

    This function:
    1. Calculates the booking period (start/end dates)
    2. Checks if remaining amount > 0
    3. Creates GL entries for the period
    4. Recursively processes additional periods if needed

    Args:
        doc: Delivery Note document
        item: Delivery Note Item row
        deferred_account: Deferred Expense Account
        expense_account: COGS/Expense Account
        posting_date: Processing date (today)
        prev_end_date: End date of previous booking (for recursion)

    Returns:
        int: Number of GL entry pairs created
    """
    # Get Accounts Settings
    accounts_settings = frappe.get_cached_doc("Accounts Settings")
    # book_based_on = accounts_settings.book_deferred_entries_based_on or "Months"
    book_based_on = "Months"
    accounts_frozen_upto = accounts_settings.acc_frozen_upto

    # Determine booking period
    start_date, end_date, is_last_entry = _get_booking_period(
        doc=doc,
        item=item,
        deferred_account=deferred_account,
        posting_date=posting_date,
        prev_end_date=prev_end_date,
    )

    if not start_date or not end_date:
        return 0

    # Get total and already booked amounts
    total_amount = flt(item.base_net_amount or item.amount)
    already_booked = _get_already_booked_amount(
        company=doc.company,
        deferred_account=deferred_account,
        dn_name=doc.name,
        item_name=item.name,
    )
    remaining_amount = flt(total_amount - already_booked, 2)

    # STOP CONDITION 1: No remaining amount
    if remaining_amount <= 0:
        frappe.logger().info(
            f"[DN Deferred Expense] DN {doc.name}, Item {item.item_code}: "
            f"Fully booked (Total: {total_amount}, Booked: {already_booked})"
        )
        return 0

    # Calculate amount to book this period
    if is_last_entry:
        # Book all remaining amount on final entry
        amount_to_book = remaining_amount
    else:
        # Calculate periodic amount
        amount_to_book = _calculate_periodic_amount(
            item=item,
            start_date=start_date,
            end_date=end_date,
            total_amount=total_amount,
            already_booked=already_booked,
            book_based_on=book_based_on,
        )

    # Ensure we don't over-book
    amount_to_book = min(flt(amount_to_book, 2), remaining_amount)

    if amount_to_book <= 0:
        return 0

    # Determine GL posting date (handle frozen books)
    gl_posting_date = end_date
    if accounts_frozen_upto and getdate(end_date) <= getdate(accounts_frozen_upto):
        gl_posting_date = get_last_day(add_days(accounts_frozen_upto, 1))

    # Create GL Entries
    entries_made = _create_gl_entry_pair(
        doc=doc,
        item=item,
        debit_account=expense_account,
        credit_account=deferred_account,
        amount=amount_to_book,
        posting_date=gl_posting_date,
    )

    # STOP CONDITION 2: This was the last entry (service end reached)
    if is_last_entry:
        return entries_made

    # Recursive catch-up if there are more periods to process
    if getdate(end_date) < getdate(posting_date):
        entries_made += _book_deferred_expense(
            doc=doc,
            item=item,
            deferred_account=deferred_account,
            expense_account=expense_account,
            posting_date=posting_date,
            prev_end_date=end_date,
        )

    return entries_made


def _get_booking_period(doc, item, deferred_account, posting_date, prev_end_date=None):
    """
    Determine the start and end dates for the current booking period.

    Args:
        doc: Delivery Note document
        item: Delivery Note Item row
        deferred_account: Deferred Expense Account
        posting_date: Processing date
        prev_end_date: End date of previous booking (for recursion)

    Returns:
        tuple: (start_date, end_date, is_last_entry)
    """
    is_last_entry = False
    service_start = getdate(item.custom_service_start_date)
    service_end = getdate(item.custom_service_end_date)

    # Check for service stop date (optional early termination)
    service_stop = item.get("custom_service_stop_date")
    if service_stop:
        service_end = min(service_end, getdate(service_stop))

    # Determine start date
    if prev_end_date:
        # Continuing from previous period
        start_date = add_days(prev_end_date, 1)
    else:
        # Check last booking date from GL
        last_booking = _get_last_booking_date(
            company=doc.company,
            deferred_account=deferred_account,
            dn_name=doc.name,
            item_name=item.name,
        )

        if last_booking:
            start_date = add_days(last_booking, 1)
        else:
            start_date = service_start

    start_date = getdate(start_date)

    # STOP CONDITION: Already past service end
    if start_date > service_end:
        return None, None, None

    # Determine end date (last day of the month containing start_date)
    end_date = get_last_day(start_date)

    # Cap at service end date
    if end_date >= service_end:
        end_date = service_end
        is_last_entry = True

    # Don't book into the future
    if end_date > getdate(posting_date):
        end_date = getdate(posting_date)

    # Validate date range
    if start_date <= end_date:
        return start_date, end_date, is_last_entry
    else:
        return None, None, None


def _calculate_periodic_amount(
    item, start_date, end_date, total_amount, already_booked, book_based_on
):
    """
    Calculate the amount to book for a period.

    Args:
        item: Delivery Note Item row
        start_date: Period start date
        end_date: Period end date
        total_amount: Total deferred amount
        already_booked: Amount already recognized
        book_based_on: 'Months' or 'Days'

    Returns:
        float: Amount to book for this period
    """
    service_start = getdate(item.custom_service_start_date)
    service_end = getdate(item.custom_service_end_date)

    # Check for service stop date
    service_stop = item.get("custom_service_stop_date")
    if service_stop:
        service_end = min(service_end, getdate(service_stop))

    total_service_days = date_diff(service_end, service_start) + 1

    if book_based_on == "Days":
        # Daily proration
        booking_days = date_diff(end_date, start_date) + 1
        if total_service_days > 0:
            amount = flt(total_amount * booking_days / total_service_days, 2)
        else:
            amount = total_amount
    else:
        # Monthly proration
        total_months = (
            (service_end.year - service_start.year) * 12
            + (service_end.month - service_start.month)
            + 1
        )

        # Calculate prorate factor for partial service periods
        denom = flt(
            date_diff(get_last_day(service_end), get_first_day(service_start)) + 1
        )
        if denom == 0:
            denom = 1

        prorate_factor = flt(total_service_days) / denom
        actual_months = rounded(total_months * prorate_factor, 1)

        if actual_months == 0:
            actual_months = 1

        # Base monthly amount
        monthly_amount = flt(total_amount / actual_months, 2)

        # Check if full month or partial month
        is_full_month = (
            get_first_day(start_date) == start_date
            and get_last_day(end_date) == end_date
        )

        if is_full_month:
            amount = monthly_amount
        else:
            # Partial month - prorate by days
            month_days = (
                date_diff(get_last_day(start_date), get_first_day(start_date)) + 1
            )
            booking_days = date_diff(end_date, start_date) + 1
            amount = flt(monthly_amount * booking_days / month_days, 2)

    # Ensure we don't exceed remaining amount
    remaining = flt(total_amount - already_booked, 2)
    return min(amount, remaining)


# =============================================================================
# GL ENTRY CREATION
# =============================================================================


def _create_gl_entry_pair(
    doc, item, debit_account, credit_account, amount, posting_date
):
    """
    Create a balanced GL Entry pair for deferred expense recognition.

    Debit: Expense Account (COGS) - Recognize expense
    Credit: Deferred Expense Account - Reduce asset

    Args:
        doc: Delivery Note document
        item: Delivery Note Item row
        debit_account: Account to debit (COGS)
        credit_account: Account to credit (Deferred Expense)
        amount: Amount to post
        posting_date: GL posting date

    Returns:
        int: 1 if successful, 0 if failed
    """
    if flt(amount) <= 0:
        return 0

    amount = flt(amount, 2)
    account_currency = get_account_currency(debit_account)
    company_currency = frappe.get_cached_value(
        "Company", doc.company, "default_currency"
    )

    # Build the GL map
    gl_map = [
        # DEBIT: Expense Account (COGS)
        frappe._dict(
            {
                "company": doc.company,
                "posting_date": posting_date,
                "account": debit_account,
                "debit": amount,
                "credit": 0,
                "debit_in_account_currency": amount,
                "credit_in_account_currency": 0,
                "against": doc.customer,
                "voucher_type": "Delivery Note",
                "voucher_no": doc.name,
                "voucher_subtype": "Delivery Note",
                "voucher_detail_no": item.name,
                "cost_center": item.cost_center,
                "project": item.get("project") or doc.get("project"),
                "remarks": _("Deferred expense recognition for {0} - {1}").format(
                    doc.name, item.item_code
                ),
                "account_currency": account_currency,
                "is_opening": "No",
                "is_advance": "No",
            }
        ),
        # CREDIT: Deferred Expense Account
        frappe._dict(
            {
                "company": doc.company,
                "posting_date": posting_date,
                "account": credit_account,
                "debit": 0,
                "credit": amount,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": amount,
                "against": doc.customer,
                "voucher_type": "Delivery Note",
                "voucher_no": doc.name,
                "voucher_subtype": "Deferred Expense",
                "voucher_detail_no": item.name,
                "cost_center": item.cost_center,
                "project": item.get("project") or doc.get("project"),
                "remarks": _("Deferred expense recognition for {0} - {1}").format(
                    doc.name, item.item_code
                ),
                "account_currency": get_account_currency(credit_account),
                "is_opening": "No",
                "is_advance": "No",
            }
        ),
    ]

    # Add accounting dimensions
    dimensions = get_accounting_dimensions()
    for entry in gl_map:
        for dimension in dimensions:
            entry[dimension] = item.get(dimension)

    try:
        # Create GL Entries using ERPNext's standard function
        make_gl_entries(gl_map, cancel=False, merge_entries=False)

        frappe.logger().info(
            f"[DN Deferred Expense] Created GL Entry pair for DN {doc.name}, "
            f"Item {item.item_code}, Amount {amount}, Date {posting_date}"
        )

        return 1

    except Exception as e:
        frappe.log_error(
            message=f"Failed to create GL Entry for DN {doc.name}, "
            f"Item {item.item_code}: {str(e)}",
            title="DN Deferred Expense GL Error",
        )
        return 0


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _get_expense_account(item, company):
    """
    Get the expense account for an item.

    Priority:
    1. Item's expense_account field
    2. Item Default for company
    3. Company's default expense account

    Args:
        item: Delivery Note Item row
        company: Company name

    Returns:
        str: Expense account name or None
    """
    # Check item row
    if item.get("expense_account"):
        return item.expense_account

    # Check item defaults
    expense_account = frappe.db.get_value(
        "Item Default",
        {"parent": item.item_code, "company": company},
        "expense_account",
    )

    if expense_account:
        return expense_account

    # Fall back to company default
    return frappe.get_cached_value("Company", company, "default_expense_account")


# =============================================================================
# MANUAL EXECUTION FUNCTIONS (for testing/debugging)
# =============================================================================


def process_deferred_expense_for_dn(dn_name, posting_date=None):
    """
    Manually process deferred expense for a specific Delivery Note.
    Useful for testing or reprocessing.

    Args:
        dn_name: Delivery Note name
        posting_date: Optional posting date (defaults to today)

    Returns:
        int: Number of GL entry pairs created
    """
    if not posting_date:
        posting_date = today()

    doc = frappe.get_doc("Delivery Note", dn_name)

    if doc.docstatus != 1:
        frappe.throw(_("Delivery Note must be submitted"))

    total_entries = 0

    for item in doc.items:
        if not item.get("custom_enable_deferred_expense"):
            continue

        if not item.get("custom_deferred_expense_account"):
            continue

        entries = _process_single_item(
            dn_name=dn_name,
            item_name=item.name,
            posting_date=posting_date,
        )

        total_entries += entries

    if total_entries > 0:
        frappe.db.commit()
        frappe.msgprint(
            _("Created {0} GL Entry pairs for {1}").format(total_entries, dn_name)
        )

    return total_entries


def get_deferred_expense_status(dn_name):
    """
    Get the deferred expense status for a Delivery Note.
    Shows total, booked, and remaining amounts per item.

    Args:
        dn_name: Delivery Note name

    Returns:
        list: Status for each deferred expense item
    """
    doc = frappe.get_doc("Delivery Note", dn_name)
    status = []

    for item in doc.items:
        if not item.get("custom_enable_deferred_expense"):
            continue

        total_amount = flt(item.base_net_amount or item.amount)
        already_booked = _get_already_booked_amount(
            company=doc.company,
            deferred_account=item.custom_deferred_expense_account,
            dn_name=doc.name,
            item_name=item.name,
        )

        status.append(
            {
                "item_code": item.item_code,
                "item_name": item.name,
                "service_start": item.custom_service_start_date,
                "service_end": item.custom_service_end_date,
                "total_amount": total_amount,
                "booked_amount": already_booked,
                "remaining_amount": flt(total_amount - already_booked, 2),
                "percentage_booked": (
                    flt(already_booked / total_amount * 100, 2) if total_amount else 0
                ),
            }
        )

    return status

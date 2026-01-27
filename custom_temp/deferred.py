import frappe
from frappe import _
from frappe.utils import add_months, get_last_day, getdate, flt, date_diff, today


def process_deferred_expenses(posting_date=None):
    """
    Scheduled job to process deferred expenses for Delivery Notes.
    - Finds Delivery Notes with deferred items active in the current period.
    - Calculates the monthly expense amount.
    - Creates a Journal Entry: Credit Deferred Expense, Debit Actual Expense.
    """
    if not posting_date:
        posting_date = today()

    # We want to process for the *previous* month usually, or the current passed month.
    # If run on Feb 1st, we want to book for Jan 31st.
    # Let's assume posting_date is the date we want to book the entry (e.g., End of Month).

    # Logic: Find items where (Start Date <= Posting Date) AND (End Date >= Posting Date)
    # Actually, standard logic is: Check if we haven't booked for this month yet.

    entries_created = 0

    # 1. Get all submitted Delivery Notes with Deferred Expense enabled
    delivery_notes = frappe.db.sql(
        """
        SELECT
            dn.name as parent,
            item.name as item_name,
            item.item_code,
            item.net_amount,
            item.amount,
            item.custom_deferred_expense_account,
            item.expense_account as item_expense_account,
            item.custom_service_start_date,
            item.custom_service_end_date
        FROM
            `tabDelivery Note` dn
        JOIN
            `tabDelivery Note Item` item ON item.parent = dn.name
        WHERE
            dn.docstatus = 1
            AND item.custom_enable_deferred_expense = 1
            AND item.custom_deferred_expense_account IS NOT NULL
            AND item.custom_service_start_date <= %s
            AND item.custom_service_end_date >= %s
    """,
        (posting_date, posting_date),
        as_dict=True,
    )

    for item in delivery_notes:
        # Fetch company explicitly if not in SQL (it wasn't), let's optimize to fetch it
        if not item.get("company"):
            item.company = frappe.db.get_value("Delivery Note", item.parent, "company")

        entries_created += process_single_item(item, posting_date)

    if entries_created:
        frappe.db.commit()

    return entries_created


def process_single_item(item, posting_date):
    """Calculates amount and creates JE for a single item"""

    # 1. Determine the Expense Account
    expense_account = get_expense_account(item)
    deferred_account = item.custom_deferred_expense_account

    if not expense_account or not deferred_account:
        return 0

    # 2. Check if already booked for this month/period
    # We check if a JE exists for this Ref Name + Ref Detail + Start of Month -> End of Month
    month_start = getdate(posting_date).replace(day=1)
    month_end = getdate(
        posting_date
    )  # Assuming posting_date is end of month, or we just check strictly

    already_booked = frappe.db.exists(
        "Journal Entry",
        {
            "voucher_type": "Deferred Expense",
            "cheque_no": item.parent,  # Linked via Cheque No for easy report fetching
            "posting_date": ("between", [month_start, month_end]),
            "docstatus": 1,
            # We might need to check specific item if multiple items per DN,
            # but usually one JE per DN or one JE per Item?
            # Let's rely on remarks or better, Reference Detail Detail
        },
    )

    # Better check using Journal Entry Account remark to be precise per item row
    already_booked_item = frappe.db.sql(
        """
        SELECT 1
        FROM `tabJournal Entry` je
        JOIN `tabJournal Entry Account` jea ON je.name = jea.parent
        WHERE je.voucher_type = 'Deferred Expense'
        AND je.docstatus = 1
        AND je.cheque_no = %s
        AND jea.user_remark LIKE %s
        AND je.posting_date BETWEEN %s AND %s
    """,
        (item.parent, f"%{item.item_name}%", month_start, month_end),
    )

    if already_booked_item:
        return 0

    # 3. Calculate Amount (Monthly Proration)
    amount = calculate_amount(item)

    if amount <= 0:
        return 0

    # 4. Create Journal Entry
    create_journal_entry(item, expense_account, deferred_account, amount, posting_date)
    return 1


def calculate_amount(item):
    """
    Calculates the monthly amortization amount for:
    Total Amount / Total Duration (Months)
    """
    total_amount = flt(item.net_amount) if item.net_amount else flt(item.amount)
    start_date = getdate(item.custom_service_start_date)
    end_date = getdate(item.custom_service_end_date)

    # Calculate Total Months (Approximate)
    diff_days = date_diff(end_date, start_date) + 1
    if diff_days <= 0:
        return 0

    # Standard 30-day month or precise?
    # Let's use precise daily rate * 30 or simply Total / Months
    # User example: 10 LYD over 24 months (01-01-2024 to 27-01-2026 approx) = 0.42

    # Exact month count approach for accounting
    # (YearDiff * 12) + MonthDiff + (DayAdjustment)

    # Simplest approach widely used: Daily Rate * Days in Request Month
    daily_rate = total_amount / diff_days

    # How many days in the booking month? (e.g. Jan has 31)
    # This might fluctuate (0.42, 0.40, 0.43).
    # User asked for 0.42 specifically (10 / 24 = 0.4166).
    # This implies a straight-line MONTHLY method, not daily.

    months = (
        (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
    )
    # Adjust for partial first/last months?
    # If Start 01-01, End 31-12, that is exactly 12 months.
    # 10 / 24 = 0.41666

    monthly_amount = total_amount / months

    return flt(monthly_amount, 2)  # Round to 2 decimals usually


def get_expense_account(item):
    if item.item_expense_account:
        return item.item_expense_account

    # Fallback to Item Default
    return frappe.db.get_value(
        "Item Default",
        {"parent": item.item_code, "company": item.company},
        "expense_account",
    )


def create_journal_entry(item, expense_account, deferred_account, amount, posting_date):
    je = frappe.new_doc("Journal Entry")
    je.company = item.company
    je.posting_date = posting_date
    je.voucher_type = "Deferred Expense"
    je.cheque_no = item.parent  # Critical for Report linking
    je.cheque_date = posting_date
    je.user_remark = f"Deferred Expense for {item.item_code} (Ref: {item.parent})"

    # Debit Expense (Increase Expense)
    # Note: We don't use reference_type/reference_name because Delivery Note
    # is not compatible with JournalEntry validation (it expects 'supplier' field).
    # Tracking is done via cheque_no, user_remark, and custom fields.
    je.append(
        "accounts",
        {
            "account": expense_account,
            "debit_in_account_currency": amount,
            "credit_in_account_currency": 0,
            "cost_center": frappe.get_cached_value(
                "Company", je.company, "cost_center"
            ),
            "user_remark": f"DN: {item.parent}, Item: {item.item_name}",
        },
    )

    # Credit Deferred Asset (Decrease Asset)
    je.append(
        "accounts",
        {
            "account": deferred_account,
            "debit_in_account_currency": 0,
            "credit_in_account_currency": amount,
            "cost_center": frappe.get_cached_value(
                "Company", je.company, "cost_center"
            ),
            "user_remark": f"DN: {item.parent}, Item: {item.item_name}",
        },
    )

    je.save(ignore_permissions=True)
    je.submit()

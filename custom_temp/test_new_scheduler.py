import frappe
from custom_temp.deferred import process_deferred_expenses


def execute():
    # TEST SCENARIO:
    # We simulate running this on Feb 1st, 2024.
    # We expect it to book the January expense for eligible items.

    posting_date = "2024-02-01"  # This serves as the "Run Date"
    # The logic in deferred.py will book separate JEs with this date or process for this period.
    # Note: If you want the JE to be dated Jan 31st, you might adjust logic in deferred.py or pass Jan 31st here.
    # For now, let's assume we book ON the run date.

    print(f"--- Running Deferred Processor for date {posting_date} ---")

    count = process_deferred_expenses(posting_date=posting_date)

    print(f"--- Completed. Created {count} Journal Entries. ---")

    if count > 0:
        jes = frappe.get_all(
            "Journal Entry",
            filters={"voucher_type": "Deferred Expense", "posting_date": posting_date},
            fields=["name", "total_debit", "user_remark"],
        )
        for je in jes:
            print(f"CREATED: {je.name} | Amount: {je.total_debit} | {je.user_remark}")


# ERPNext / Frappe (server-side) — recommended way to create GL Entries
import frappe
from frappe.utils import nowdate, flt
from erpnext.accounts.general_ledger import make_gl_entries


def create_gl_entry_pair_for_delivery_note_deferred_expense(
    company: str,
    posting_date: str,
    debit_account: str,
    credit_account: str,
    amount: float,
    voucher_type: str,
    voucher_no: str,
    voucher_subtype: str,
    transaction_date: str | None = None,
    fiscal_year: str | None = None,
    due_date: str | None = None,
    cost_center: str | None = None,
    party_type: str | None = None,
    party: str | None = None,
    remarks: str | None = None,
    project: str | None = None,
    against: str | None = None,
    account_currency: str | None = None,
    is_opening: str = "No",
    is_advance: str = "No",
):
    """
    Creates a balanced (Debit/Credit) GL posting for delivery note deferred expense.
    This is a monthly long scheduled job
    """
    amount = flt(amount)
    if amount <= 0:
        frappe.throw("Amount must be > 0")

    total_debit = amount
    total_credit = amount
    if flt(total_debit - total_credit, 2) != 0:
        frappe.throw("GL is not balanced")

    # wrap the dictionaries in frappe._dict so dot-notation access works
    gl_map = [
        frappe._dict(
            {
                "company": company,
                "posting_date": posting_date,
                "voucher_type": voucher_type,
                "voucher_no": voucher_no,
                "voucher_subtype": voucher_subtype,
                "account": debit_account,
                "debit": amount,
                "credit": 0,
                "debit_in_account_currency": amount,
                "credit_in_account_currency": 0,
                "cost_center": cost_center,
                "party_type": party_type,
                "party": party,
                "remarks": remarks,
                "project": project,
                "against": against,
                "account_currency": account_currency,
                "is_opening": is_opening,
                "is_advance": is_advance,
            }
        ),
        frappe._dict(
            {
                "company": company,
                "posting_date": posting_date,
                "voucher_type": voucher_type,
                "voucher_no": voucher_no,
                "voucher_subtype": voucher_subtype,
                "account": credit_account,
                "debit": 0,
                "credit": amount,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": amount,
                "cost_center": cost_center,
                "party_type": party_type,
                "party": party,
                "remarks": remarks,
                "project": project,
                "against": against,
                "account_currency": account_currency,
                "is_opening": is_opening,
                "is_advance": is_advance,
            }
        ),
    ]

    # This will create GL Entry records (and run the proper ledger logic)
    make_gl_entries(gl_map, cancel=False, merge_entries=False)

    # Commit is usually required if running from console/script,
    # but be careful if running inside another transaction.
    frappe.db.commit()

    return gl_map


# --- Execution ---
create_gl_entry_pair(
    company="Catfa (Demo)",
    posting_date="2024-01-31",  # Ensure YYYY-MM-DD format usually
    debit_account="Cost of Goods Sold - CD",
    credit_account="Network Assets - CD",
    amount=7 / 24,
    voucher_type="Delivery Note",
    voucher_no="MAT-DN-2026-00039",
    cost_center="Main - CD",  # Better to pass None than empty string ""
    remarks="Manual Entry via Script",
    project=None,
    voucher_subtype="Delivery Note",
    against="Grant Plastics Ltd.",
)

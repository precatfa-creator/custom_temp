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

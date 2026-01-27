"""
Custom Delivery Note Class for Deferred Expense Handling

This class overrides the standard Delivery Note to:
1. Redirect the initial COGS posting to the Deferred Expense Account
2. Support the deferred expense recognition flow similar to Purchase Invoice

The flow is:
1. On DN submit: Stock Account → Deferred Expense Account (instead of COGS)
2. Monthly scheduler: Deferred Expense Account → COGS (via Journal Entries)
"""

import frappe
from frappe import _
from frappe.utils import flt
from erpnext.stock.doctype.delivery_note.delivery_note import DeliveryNote


class CustomDeliveryNote(DeliveryNote):
    """
    Custom Delivery Note class that handles deferred expense accounting.

    When a Delivery Note item has deferred expense enabled:
    - The GL entry is modified to post to the Deferred Expense Account
      instead of the regular expense/COGS account
    - This mirrors the Purchase Invoice deferred expense behavior
    """

    def validate(self):
        """Validate the document and check deferred expense settings."""
        super().validate()
        self.validate_deferred_expense_accounts()

    def validate_deferred_expense_accounts(self):
        """Ensure deferred expense items have all required fields."""
        for item in self.items:
            if item.get("custom_enable_deferred_expense"):
                if not item.get("custom_deferred_expense_account"):
                    frappe.throw(
                        _(
                            "Row {0}: Deferred Expense Account is required when "
                            "Enable Deferred Expense is checked for item {1}"
                        ).format(item.idx, item.item_code)
                    )

                if not item.get("custom_service_start_date"):
                    frappe.throw(
                        _(
                            "Row {0}: Service Start Date is required when "
                            "Enable Deferred Expense is checked for item {1}"
                        ).format(item.idx, item.item_code)
                    )

                if not item.get("custom_service_end_date"):
                    frappe.throw(
                        _(
                            "Row {0}: Service End Date is required when "
                            "Enable Deferred Expense is checked for item {1}"
                        ).format(item.idx, item.item_code)
                    )

                # Validate dates
                if item.get("custom_service_start_date") and item.get(
                    "custom_service_end_date"
                ):
                    if item.custom_service_start_date > item.custom_service_end_date:
                        frappe.throw(
                            _(
                                "Row {0}: Service Start Date cannot be after "
                                "Service End Date for item {1}"
                            ).format(item.idx, item.item_code)
                        )

    def on_submit(self):
        """Hook after submit to create deferred expense Journal Entry."""
        super().on_submit()

        # Check if any items have deferred expense enabled
        has_deferred = False
        for item in self.items:
            if item.get("custom_enable_deferred_expense") and item.get(
                "custom_deferred_expense_account"
            ):
                has_deferred = True
                break

        if has_deferred:
            je = frappe.new_doc("Journal Entry")
            je.posting_date = self.posting_date
            je.company = self.company
            je.voucher_type = "Journal Entry"
            je.user_remark = (
                f"Deferred Expense Reclassification from Delivery Note {self.name}"
            )
            je.cheque_no = self.name
            je.cheque_date = self.posting_date

            for item in self.items:
                if item.get("custom_enable_deferred_expense") and item.get(
                    "custom_deferred_expense_account"
                ):
                    amount = item.amount or (item.qty * item.rate)

                    expense_account = item.expense_account
                    if not expense_account:
                        expense_account = frappe.db.get_value(
                            "Item Default",
                            {"parent": item.item_code, "company": self.company},
                            "expense_account",
                        )

                    if not expense_account:
                        expense_account = frappe.db.get_value(
                            "Company", self.company, "default_expense_account"
                        )

                    # Debit Deferred Expense Account
                    je.append(
                        "accounts",
                        {
                            "account": item.custom_deferred_expense_account,
                            "debit_in_account_currency": amount,
                            "credit_in_account_currency": 0,
                            "reference_type": "Delivery Note",
                            "reference_name": self.name,
                            "user_remark": f"DN: {self.name} | Item: {item.item_code}",
                        },
                    )

                    # Credit COGS/Expense Account
                    je.append(
                        "accounts",
                        {
                            "account": expense_account,
                            "debit_in_account_currency": 0,
                            "credit_in_account_currency": amount,
                            "reference_type": "Delivery Note",
                            "reference_name": self.name,
                            "user_remark": f"DN: {self.name} | Item: {item.item_code}",
                        },
                    )

            if je.accounts:
                je.insert(ignore_permissions=True)
                je.submit()
                frappe.msgprint(
                    _(f"Deferred Expense Journal Entry {je.name} created"), alert=True
                )

                frappe.logger("deferred_expense").info(
                    f"Delivery Note {self.name}: Created Deferred Expense JE {je.name}"
                )

    def on_cancel(self):
        """Cancel related deferred expense Journal Entries."""
        super().on_cancel()
        self.cancel_deferred_expense_journal_entries()

    def cancel_deferred_expense_journal_entries(self):
        """Find and cancel Journal Entries created for deferred expense."""
        # Find JEs by reference
        journal_entries = frappe.db.sql(
            """
            SELECT DISTINCT p.name
            FROM `tabJournal Entry` p
            INNER JOIN `tabJournal Entry Account` c ON p.name = c.parent
            WHERE c.reference_type = 'Delivery Note'
            AND c.reference_name = %s
            AND p.docstatus = 1
            AND p.voucher_type = 'Journal Entry'
            AND p.user_remark LIKE 'Deferred Expense Reclassification %%'
        """,
            (self.name,),
            as_dict=True,
        )

        cancelled_count = 0
        for je_row in journal_entries:
            try:
                je = frappe.get_doc("Journal Entry", je_row.name)
                if je.docstatus == 1:
                    je.cancel()
                    cancelled_count += 1
                    frappe.logger("deferred_expense").info(
                        f"Cancelled Deferred Expense JE {je.name} for DN {self.name}"
                    )
            except Exception as e:
                frappe.log_error(
                    message=f"Error cancelling JE {je_row.name} for DN {self.name}: {str(e)}",
                    title="Deferred Expense JE Cancel Error",
                )

        if cancelled_count > 0:
            frappe.msgprint(
                _("{0} Deferred Expense Journal Entries cancelled").format(
                    cancelled_count
                ),
                alert=True,
            )

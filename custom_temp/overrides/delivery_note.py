"""
Custom Delivery Note Class for Deferred Expense Handling

This class overrides the standard Delivery Note to force GL entries
even if Perpetual Inventory is disabled, simulating Purchase Invoice logic.
"""

import frappe
from frappe import _
from frappe.utils import flt
from erpnext.stock.doctype.delivery_note.delivery_note import DeliveryNote
from erpnext.accounts.general_ledger import make_gl_entries, make_reverse_gl_entries


class CustomDeliveryNote(DeliveryNote):
    """
    Custom Delivery Note class that handles deferred expense accounting.
    Forces GL entries even when Perpetual Inventory is OFF.
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

    def on_submit(self):
        """
        Override on_submit to explicitly call make_gl_entries.
        The standard Delivery Note has this line COMMENTED OUT.
        """
        super().on_submit()

        # Only create GL entries if they don't already exist for this voucher
        existing_entries = frappe.db.count(
            "GL Entry",
            filters={
                "voucher_type": self.doctype,
                "voucher_no": self.name,
                "is_cancelled": 0,
            },
        )

        if existing_entries == 0:
            self.make_gl_entries()

    def make_gl_entries(self, gl_entries=None, from_repost=False):
        """
        Forces GL entry creation even if Perpetual Inventory is off.
        """
        if self.docstatus == 1:
            if not gl_entries:
                gl_entries = self.get_gl_entries()

            if gl_entries:
                make_gl_entries(gl_entries, from_repost=from_repost)

        elif self.docstatus == 2:
            make_reverse_gl_entries(voucher_type=self.doctype, voucher_no=self.name)

    def get_gl_entries(self, warehouse_account=None):
        """
        Creates GL entries for deferred expense items.
        Bypasses the standard check_expense_account validation that requires P&L accounts.
        """
        from erpnext.stock import get_warehouse_account_map
        from erpnext.accounts.utils import get_account_currency

        if not warehouse_account:
            warehouse_account = get_warehouse_account_map(self.company)

        gl_entries = []

        # Check if we have any deferred expense items
        has_deferred_items = any(
            item.get("custom_enable_deferred_expense") for item in self.items
        )

        if has_deferred_items and not self.is_return:
            # Build entries manually to avoid check_expense_account validation
            for item in self.items:

                # FIX 1: Get the warehouse account (Credit side)
                # item_warehouse_account = warehouse_account.get(
                # 	item.warehouse, {}
                # ).get("account")

                if item.get("custom_enable_deferred_expense"):
                    deferred_account = item.get("custom_deferred_expense_account")
                    expense_account = item.expense_account

                    if not expense_account:
                        expense_account = frappe.get_cached_value(
                            "Company", self.company, "default_expense_account"
                        )

                    if deferred_account and expense_account:
                        amount = flt(item.base_net_amount)

                        if amount > 0:
                            # Debit Deferred Expense Account (Asset - Balance Sheet)
                            gl_entries.append(
                                self.get_gl_dict(
                                    {
                                        "account": deferred_account,
                                        "against": self.customer,
                                        "debit": amount,
                                        "debit_in_account_currency": amount,
                                        "cost_center": item.cost_center,
                                        "project": item.project or self.project,
                                        "voucher_detail_no": "",  # KEY: Links to specific item row for tracking!
                                        "remarks": _("Deferred expense for {0}").format(
                                            item.item_code
                                        ),
                                    },
                                    get_account_currency(deferred_account),
                                    item=item,
                                )
                            )

                            # CREDIT: Warehouse Account (Stock Asset) - FIXED!
                            gl_entries.append(
                                self.get_gl_dict(
                                    {
                                        "account": warehouse_account.get(
                                            item.warehouse, {}
                                        ).get(
                                            "account"
                                        ),  # Fixed!
                                        "against": self.customer,
                                        "credit": amount,
                                        "credit_in_account_currency": amount,
                                        "cost_center": item.cost_center,
                                        "project": item.project or self.project,
                                        "voucher_detail_no": "",  # KEY: Links to specific item row for tracking!
                                        "remarks": _("Deferred expense for {0}").format(
                                            item.item_code
                                        ),
                                    },
                                    get_account_currency(
                                        warehouse_account.get(item.warehouse, {}).get(
                                            "account"
                                        )
                                    ),
                                    item=item,
                                )
                            )

            return gl_entries

        # For non-deferred items, try standard logic (if Perpetual is ON)
        try:
            gl_entries = super().get_gl_entries(warehouse_account)
        except Exception:
            gl_entries = []

        return gl_entries

    def on_cancel(self):
        super().on_cancel()
        # Reverse GL entries on cancel
        self.make_gl_entries()
        self.cancel_deferred_expense_journal_entries()

    def cancel_deferred_expense_journal_entries(self):
        jes = frappe.get_all(
            "Journal Entry",
            filters={
                "cheque_no": self.name,
                "voucher_type": "Deferred Expense",
                "docstatus": 1,
            },
            pluck="name",
        )
        for je_name in jes:
            try:
                frappe.get_doc("Journal Entry", je_name).cancel()
            except Exception:
                pass

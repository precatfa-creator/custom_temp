import frappe
from frappe import _
from frappe.utils import today, add_months, add_days
from custom_temp.tasks import process_delivery_note_deferred_expense


@frappe.whitelist()
def run_delivery_note_deferred_expense(posting_date=None):
    """
    Manual trigger for processing deferred expense for Delivery Notes.
    Similar to ERPNext's process_deferred_accounting function.

    This can be called from:
    1. Scheduler (automatic monthly processing)
    2. Manually via API call
    3. From a button in the UI
    """
    if not posting_date:
        posting_date = today()

    count = process_delivery_note_deferred_expense(posting_date=posting_date)

    if count > 0:
        frappe.msgprint(
            _("{0} Deferred Expense Journal Entries created for Delivery Notes").format(
                count
            ),
            alert=True,
        )
    else:
        frappe.msgprint(
            _("No pending deferred expense entries to process for Delivery Notes"),
            alert=True,
        )

    return count


@frappe.whitelist()
def process_single_delivery_note_deferred_expense(
    delivery_note_name, posting_date=None
):
    """
    Process deferred expense for a single Delivery Note.
    This can be called from the Delivery Note form via a button.
    """
    from custom_temp.tasks import book_deferred_expense_recursive

    if not posting_date:
        posting_date = today()

    doc = frappe.get_doc("Delivery Note", delivery_note_name)

    if doc.docstatus != 1:
        frappe.throw(_("Delivery Note must be submitted to process deferred expense"))

    count = 0
    for item in doc.items:
        if item.get("custom_enable_deferred_expense") and item.get(
            "custom_deferred_expense_account"
        ):
            count += book_deferred_expense_recursive(doc, item, posting_date)

    if count > 0:
        frappe.db.commit()
        frappe.msgprint(
            _("{0} Deferred Expense Journal Entries created for {1}").format(
                count, delivery_note_name
            ),
            alert=True,
        )
    else:
        frappe.msgprint(
            _("No pending deferred expense entries to process for {0}").format(
                delivery_note_name
            ),
            alert=True,
        )

    return count


def after_submit_delivery_note(doc, method):
    """
    Hook function called after Delivery Note is submitted.
    The GL entry modification is handled in the CustomDeliveryNote class.
    This function can be used for any additional post-submit processing.
    """
    # Check if any item has deferred expense enabled
    has_deferred = False
    deferred_items = []
    for item in doc.items:
        if item.get("custom_enable_deferred_expense") and item.get(
            "custom_deferred_expense_account"
        ):
            has_deferred = True
            deferred_items.append(item.item_code)

    if has_deferred:
        frappe.msgprint(
            _(
                "Delivery Note {0} contains deferred expense items: {1}. "
                "Deferred expense will be recognized according to the service period."
            ).format(doc.name, ", ".join(deferred_items)),
            alert=True,
        )


def after_cancel_delivery_note(doc, method):
    """
    Hook function called after Delivery Note is cancelled.
    Cancel related Journal Entries created for deferred expense.
    """
    # Find Journal Entries linked to this Delivery Note
    journal_entries = frappe.db.sql(
        """
        SELECT DISTINCT p.name
        FROM `tabJournal Entry` p
        INNER JOIN `tabJournal Entry Account` c ON p.name = c.parent
        WHERE c.reference_type = 'Delivery Note'
        AND c.reference_name = %s
        AND p.docstatus = 1
    """,
        (doc.name,),
        as_dict=True,
    )

    # Also check cheque_no field for backward compatibility
    je_by_cheque = frappe.get_all(
        "Journal Entry",
        filters={
            "cheque_no": doc.name,
            "voucher_type": "Deferred Expense",
            "docstatus": 1,
        },
        pluck="name",
    )

    all_jes = set([je.name for je in journal_entries] + je_by_cheque)

    cancelled_count = 0
    for je_name in all_jes:
        je = frappe.get_doc("Journal Entry", je_name)
        if je.docstatus == 1:
            je.cancel()
            cancelled_count += 1

    if cancelled_count > 0:
        frappe.msgprint(
            _("{0} Deferred Expense Journal Entries cancelled for {1}").format(
                cancelled_count, doc.name
            ),
            alert=True,
        )

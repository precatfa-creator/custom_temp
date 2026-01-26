/**
 * Delivery Note Client Script for Deferred Expense
 *
 * This script auto-populates deferred expense fields when an item is selected
 * in the Delivery Note Item table, similar to how Purchase Invoice handles it.
 */

frappe.ui.form.on("Delivery Note", {
	setup: function (frm) {
		frm.set_query("custom_deferred_expense_account", "items", function (doc, cdt, cdn) {
			return {
				filters: {
					root_type: "Asset",
					company: frm.doc.company,
					is_group: 0,
				},
			};
		});
	},
	refresh: function (frm) {
		// Add button to manually process deferred expense
		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(
				__("Process Deferred Expense"),
				function () {
					frappe.call({
						method: "custom_temp.api.process_single_delivery_note_deferred_expense",
						args: {
							delivery_note_name: frm.doc.name,
							posting_date: frm.doc.posting_date,
						},
						freeze: true,
						freeze_message: __("Processing Deferred Expense..."),
						callback: function (r) {
							if (r.message) {
								frm.reload_doc();
							}
						},
					});
				},
				__("Actions"),
			);
		}
	},
});

frappe.ui.form.on("Delivery Note Item", {
	item_code: function (frm, cdt, cdn) {
		let row = locals[cdt][cdn];

		if (!row.item_code) return;

		frappe.call({
			method: "get_item_deferred_details",
			args: {
				item_code: row.item_code,
				company: frm.doc.company,
			},
			callback: function (r) {
				if (r.message && r.message.enable_deferred_expense) {
					let data = r.message;

					frappe.model.set_value(cdt, cdn, "custom_enable_deferred_expense", 1);
					frappe.model.set_value(
						cdt,
						cdn,
						"custom_deferred_expense_account",
						data.deferred_expense_account,
					);
					frappe.model.set_value(
						cdt,
						cdn,
						"custom_service_start_date",
						frm.doc.posting_date,
					);

					if (frm.doc.posting_date && data.no_of_months_exp) {
						let end_date = frappe.datetime.add_months(
							frm.doc.posting_date,
							data.no_of_months_exp,
						);
						frappe.model.set_value(cdt, cdn, "custom_service_end_date", end_date);
					}
				} else {
					frappe.model.set_value(cdt, cdn, "custom_enable_deferred_expense", 0);
					frappe.model.set_value(cdt, cdn, "custom_deferred_expense_account", "");
					frappe.model.set_value(cdt, cdn, "custom_service_start_date", "");
					frappe.model.set_value(cdt, cdn, "custom_service_end_date", "");
				}
			},
		});
	},
});

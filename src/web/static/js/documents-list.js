/* documents-list.js — Bulk operations management for the documents list.
 *
 * Extracted from documents/list.html inline <script> block (ADR-003).
 * Loaded via {% block extra_js %} in documents/list.html.
 *
 * Functions are exposed globally because the HTML uses inline onclick handlers.
 *
 * Manages:
 * - Select All checkbox toggle
 * - Bulk action button enable/disable state
 * - Syncing selected doc_ids across all bulk operation forms (delete, tag, move, export)
 */

function toggleSelectAll(selectAllCheckbox) {
    var checkboxes = document.querySelectorAll('.doc-checkbox');
    for (var i = 0; i < checkboxes.length; i++) {
        checkboxes[i].checked = selectAllCheckbox.checked;
    }
    updateBulkActionButtons();
}

function updateDeleteButton() {
    updateBulkActionButtons();
}

/* Get the comma-separated list of checked doc IDs — used for the
   bulk-export GET form which needs doc_ids as a string. */
function getCheckedDocIds() {
    var checked = document.querySelectorAll('.doc-checkbox:checked');
    var ids = [];
    for (var i = 0; i < checked.length; i++) {
        ids.push(checked[i].value);
    }
    return ids;
}

/* Update all bulk action buttons and hidden doc_ids fields based on
   the current checkbox selection state. */
function updateBulkActionButtons() {
    var checked = document.querySelectorAll('.doc-checkbox:checked');
    var count = checked.length;
    var ids = [];
    for (var i = 0; i < checked.length; i++) {
        ids.push(checked[i].value);
    }
    var idsString = ids.join(',');
    var countSpan = document.getElementById('selected-count');
    if (countSpan) {
        countSpan.textContent = count;
    }

    // Enable/disable all bulk action buttons
    var buttonIds = [
        'delete-selected-btn',
        'tag-selected-btn',
        'move-selected-btn',
        'export-selected-btn'
    ];
    var enabled = count > 0;
    for (var i = 0; i < buttonIds.length; i++) {
        var btn = document.getElementById(buttonIds[i]);
        if (btn) {
            btn.disabled = !enabled;
            btn.style.cursor = enabled ? 'pointer' : 'not-allowed';
            btn.style.opacity = enabled ? '1' : '0.5';
        }
    }

    // Enable/disable the collection select and format select
    var moveSelect = document.getElementById('bulk-move-select');
    if (moveSelect) {
        moveSelect.disabled = !enabled;
    }
    var exportFormat = document.getElementById('bulk-export-format');
    if (exportFormat) {
        exportFormat.disabled = !enabled;
    }

    // Sync hidden doc_ids fields in tag, move, and export forms
    var hiddenIds = ['bulk-tag-doc-ids', 'bulk-move-doc-ids', 'bulk-export-doc-ids'];
    for (var i = 0; i < hiddenIds.length; i++) {
        var hidden = document.getElementById(hiddenIds[i]);
        if (hidden) {
            hidden.value = idsString;
        }
    }

    // Update select-all checkbox state
    var selectAll = document.getElementById('select-all');
    if (selectAll) {
        var allCheckboxes = document.querySelectorAll('.doc-checkbox');
        selectAll.checked = allCheckboxes.length > 0 && checked.length === allCheckboxes.length;
    }
}

function confirmBulkDelete() {
    var count = document.querySelectorAll('.doc-checkbox:checked').length;
    if (count === 0) {
        return false;
    }
    return confirm('Are you sure you want to delete ' + count + ' document(s)? This action cannot be undone.');
}

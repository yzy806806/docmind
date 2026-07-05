/* documents-list.js — Bulk-delete checkbox management for the documents list.
 *
 * Extracted from documents/list.html inline <script> block.
 * Loaded via {% block extra_js %} in documents/list.html.
 *
 * Functions are exposed globally because the HTML uses inline onclick handlers.
 */
function toggleSelectAll(selectAllCheckbox) {
    var checkboxes = document.querySelectorAll('.doc-checkbox');
    for (var i = 0; i < checkboxes.length; i++) {
        checkboxes[i].checked = selectAllCheckbox.checked;
    }
    updateDeleteButton();
}

function updateDeleteButton() {
    var checked = document.querySelectorAll('.doc-checkbox:checked');
    var btn = document.getElementById('delete-selected-btn');
    var countSpan = document.getElementById('selected-count');
    var selectAll = document.getElementById('select-all');
    countSpan.textContent = checked.length;
    if (checked.length > 0) {
        btn.disabled = false;
        btn.style.cursor = 'pointer';
        btn.style.opacity = '1';
    } else {
        btn.disabled = true;
        btn.style.cursor = 'not-allowed';
        btn.style.opacity = '0.5';
    }
    // Update select-all checkbox state
    var allCheckboxes = document.querySelectorAll('.doc-checkbox');
    selectAll.checked = allCheckboxes.length > 0 && checked.length === allCheckboxes.length;
}

function confirmBulkDelete() {
    var count = document.querySelectorAll('.doc-checkbox:checked').length;
    if (count === 0) {
        return false;
    }
    return confirm('Are you sure you want to delete ' + count + ' document(s)? This action cannot be undone.');
}

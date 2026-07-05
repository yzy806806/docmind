/* faceted-filters.js — Faceted search UI for the documents list.
 *
 * Provides:
 *   - Date-range quick-preset buttons (7d, 30d, 90d, 1y) that set
 *     date_from/date_to inputs and auto-submit the filter form.
 *   - Auto-submit on facet <select> change (HTMX handles the swap,
 *     but we also trigger form submit for non-HTMX fallback).
 *
 * Loaded via {% block extra_js %} in documents/list.html.
 * Depends on HTMX being present (progressive enhancement — without
 * HTMX the form still works as a normal GET submit).
 */
(function () {
    "use strict";

    function setDateRange(days) {
        var now = new Date();
        var from = new Date();
        from.setDate(now.getDate() - days);

        var fromInput = document.querySelector('input[name="date_from"]');
        var toInput = document.querySelector('input[name="date_to"]');
        if (!fromInput || !toInput) return;

        fromInput.value = from.toISOString().slice(0, 10);
        toInput.value = now.toISOString().slice(0, 10);
    }

    function submitForm() {
        var form = document.getElementById('facet-filter-form');
        if (!form) return;
        // If HTMX is present, it intercepts the submit event via
        // hx-trigger="submit, change" and does a partial swap.
        // If HTMX is absent, the native form GET fires (full reload).
        form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
    }

    // Wire up date-preset buttons
    document.addEventListener('DOMContentLoaded', function () {
        var presetButtons = document.querySelectorAll('.date-preset-btn');
        for (var i = 0; i < presetButtons.length; i++) {
            presetButtons[i].addEventListener('click', function () {
                var days = parseInt(this.getAttribute('data-days'), 10);
                if (isNaN(days)) return;
                setDateRange(days);
                submitForm();
            });
        }

        // Auto-submit when a facet select changes
        var facetSelects = document.querySelectorAll('.facet-select');
        for (var j = 0; j < facetSelects.length; j++) {
            facetSelects[j].addEventListener('change', function () {
                submitForm();
            });
        }
    });
})();

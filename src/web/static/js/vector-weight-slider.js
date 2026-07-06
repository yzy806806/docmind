/* vector-weight-slider.js — UI logic for the hybrid search vector_weight slider.
 *
 * On any page with a .vw-slider element, this script:
 *   1. Syncs the displayed value (.vw-value) as the user drags the slider.
 *   2. On the search results page, updates the export links to carry the
 *      current vector_weight so CSV/JSON exports reflect the user's tuning.
 *
 * The slider is a progressive-enhancement layer — the form still works
 * as a plain GET submit even if JS fails to load (the name="vector_weight"
 * input is submitted with the form).
 */
(function () {
    "use strict";

    function init() {
        var sliders = document.querySelectorAll(".vw-slider");
        if (sliders.length === 0) return;

        sliders.forEach(function (slider) {
            // Find the sibling .vw-value display element
            var control = slider.closest(".vector-weight-control");
            if (!control) return;
            var valueDisplay = control.querySelector(".vw-value");
            var defaultVal = valueDisplay
                ? parseFloat(valueDisplay.getAttribute("data-default") || "0.6")
                : 0.6;

            function updateDisplay() {
                var val = parseFloat(slider.value);
                if (valueDisplay) {
                    valueDisplay.textContent = val.toFixed(2);
                }
                updateExportLinks(val);
            }

            // Update export links on the search results page
            function updateExportLinks(val) {
                var exportLinks = document.querySelectorAll(
                    ".search-export-bar .btn-export"
                );
                exportLinks.forEach(function (link) {
                    var href = link.getAttribute("href");
                    // Replace or add vector_weight= param in the href
                    if (href.indexOf("vector_weight=") !== -1) {
                        href = href.replace(
                            /vector_weight=[\d.]+/,
                            "vector_weight=" + val.toFixed(2)
                        );
                    } else {
                        href = href + "&vector_weight=" + val.toFixed(2);
                    }
                    link.setAttribute("href", href);
                });
            }

            // Set initial display from the slider's current value
            // (which may differ from default on the results page)
            updateDisplay();

            // Update on user interaction
            slider.addEventListener("input", updateDisplay);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();

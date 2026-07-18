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

    // Graceful fallback if perf-utils.js isn't loaded
    var _perf = window.DocMindPerf || {};
    var rAFThrottle = _perf.rAFThrottle || function (fn) {
        var scheduled = false, lastArgs, lastThis;
        return function () {
            lastArgs = arguments; lastThis = this;
            if (scheduled) return;
            scheduled = true;
            (window.requestAnimationFrame || function (cb) { return setTimeout(cb, 0); })(function () {
                scheduled = false;
                fn.apply(lastThis, lastArgs);
            });
        };
    };

    function init() {
        var sliders = document.querySelectorAll(".vw-slider");
        if (sliders.length === 0) return;

        sliders.forEach(function (slider) {
            var control = slider.closest(".vector-weight-control");
            if (!control) return;
            var valueDisplay = control.querySelector(".vw-value");
            var defaultVal = valueDisplay
                ? parseFloat(valueDisplay.getAttribute("data-default") || "0.6")
                : 0.6;

            function updateExportLinks(val) {
                var exportLinks = document.querySelectorAll(
                    ".search-export-bar .btn-export"
                );
                exportLinks.forEach(function (link) {
                    var href = link.getAttribute("href");
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

            // rAF-throttled display update — coalesces rapid slider input
            // events into one DOM write per animation frame, avoiding layout
            // thrashing from repeated textContent + setAttribute calls.
            var updateDisplay = rAFThrottle(function () {
                var val = parseFloat(slider.value);
                var valText = val.toFixed(2);
                if (valueDisplay) {
                    valueDisplay.textContent = valText;
                }
                slider.setAttribute("aria-valuenow", valText);
                slider.setAttribute("aria-valuetext", valText);
                updateExportLinks(val);
            });

            updateDisplay();
            slider.addEventListener("input", updateDisplay);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();

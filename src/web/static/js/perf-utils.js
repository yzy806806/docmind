/* perf-utils.js — Shared debounce/throttle/rAF helpers. */
(function () {
    "use strict";
    function debounce(fn, wait) {
        var timer = null;
        return function () {
            var ctx = this, args = arguments;
            clearTimeout(timer);
            timer = setTimeout(function () { fn.apply(ctx, args); }, wait);
        };
    }
    function throttle(fn, limit) {
        var inThrottle = false;
        return function () {
            var ctx = this, args = arguments;
            if (!inThrottle) {
                fn.apply(ctx, args);
                inThrottle = true;
                setTimeout(function () { inThrottle = false; }, limit);
            }
        };
    }
    function rAF(fn) {
        if (window.requestAnimationFrame) return window.requestAnimationFrame(fn);
        return setTimeout(fn, 16);
    }
    window.DocMindPerf = { debounce: debounce, throttle: throttle, rAF: rAF };
})();

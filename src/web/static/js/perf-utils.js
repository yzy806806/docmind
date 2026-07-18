/* perf-utils.js — Shared debounce, throttle, and rAF-throttle utilities.
 *
 * Provides three rate-limiting helpers used across the DocMind "islands":
 *
 *   DocMindPerf.debounce(fn, wait)
 *     Delay fn until `wait` ms have elapsed since the last call.
 *     Use for: search-as-you-type, autosave, form-submit-on-change.
 *
 *   DocMindPerf.throttle(fn, wait)
 *     Call fn at most once per `wait` ms, always firing the final call
 *     after the tail window. Use for: scroll, resize, XHR progress events
 *     where you want periodic updates without flooding.
 *
 *   DocMindPerf.rAFThrottle(fn)
 *     Call fn at most once per animation frame (≤ 60 fps). Use for:
 *     visual/DOM-mutation updates triggered by high-frequency events
 *     (slider input, streaming text append) — smoother than a fixed ms
 *     throttle and automatically pauses when the tab is hidden.
 *
 * Design notes:
 * - Vanilla JS, no dependencies, IIFE — follows the islands convention.
 * - Exposed as window.DocMindPerf so other modules can consume it.
 * - Each wrapper preserves `this` and forwards arguments.
 * - All wrappers provide a .cancel() method to clear pending timers/frames.
 */
(function () {
    "use strict";

    function debounce(fn, wait) {
        var timer = null;
        var lastArgs = null;
        var lastThis = null;
        function wrapped() {
            lastArgs = arguments;
            lastThis = this;
            if (timer) clearTimeout(timer);
            timer = setTimeout(function () {
                timer = null;
                fn.apply(lastThis, lastArgs);
                lastArgs = null;
                lastThis = null;
            }, wait);
        }
        wrapped.cancel = function () {
            if (timer) { clearTimeout(timer); timer = null; lastArgs = null; lastThis = null; }
        };
        return wrapped;
    }

    function throttle(fn, wait) {
        var lastCall = 0;
        var timer = null;
        var lastArgs = null;
        var lastThis = null;
        function wrapped() {
            var now = Date.now();
            var remaining = wait - (now - lastCall);
            lastArgs = arguments;
            lastThis = this;
            if (remaining <= 0) {
                if (timer) { clearTimeout(timer); timer = null; }
                lastCall = now;
                fn.apply(lastThis, lastArgs);
                lastArgs = null;
                lastThis = null;
            } else if (!timer) {
                timer = setTimeout(function () {
                    timer = null;
                    lastCall = Date.now();
                    fn.apply(lastThis, lastArgs);
                    lastArgs = null;
                    lastThis = null;
                }, remaining);
            }
        }
        wrapped.cancel = function () {
            if (timer) { clearTimeout(timer); timer = null; }
            lastCall = 0; lastArgs = null; lastThis = null;
        };
        return wrapped;
    }

    function rAFThrottle(fn) {
        var scheduled = false;
        var lastArgs = null;
        var lastThis = null;
        var rafId = null;
        var scheduleNext = (typeof requestAnimationFrame === "function")
            ? function (cb) { return requestAnimationFrame(cb); }
            : function (cb) { return setTimeout(cb, 0); };
        var cancelNext = (typeof cancelAnimationFrame === "function")
            ? function (id) { return cancelAnimationFrame(id); }
            : function (id) { return clearTimeout(id); };
        function wrapped() {
            lastArgs = arguments;
            lastThis = this;
            if (scheduled) return;
            scheduled = true;
            rafId = scheduleNext(function () {
                scheduled = false;
                rafId = null;
                fn.apply(lastThis, lastArgs);
                lastArgs = null;
                lastThis = null;
            });
        }
        wrapped.cancel = function () {
            if (rafId !== null) { cancelNext(rafId); rafId = null; }
            scheduled = false; lastArgs = null; lastThis = null;
        };
        return wrapped;
    }

    window.DocMindPerf = {
        debounce: debounce,
        throttle: throttle,
        rAFThrottle: rAFThrottle
    };
})();

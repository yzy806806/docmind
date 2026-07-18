/* progress-bar.js — Top progress bar for HTMX and fetch requests. */
(function () {
    "use strict";
    var bar = null, activeRequests = 0, completeTimer = null;
    function getBar() { if (!bar) bar = document.getElementById('progress-bar'); return bar; }
    function start() {
        var b = getBar(); if (!b) return;
        activeRequests++;
        if (completeTimer) { clearTimeout(completeTimer); completeTimer = null; }
        b.classList.remove('complete'); b.classList.add('active');
    }
    function stop() {
        var b = getBar(); if (!b) return;
        activeRequests--;
        if (activeRequests > 0) return;
        if (activeRequests < 0) activeRequests = 0;
        b.classList.remove('active'); b.classList.add('complete');
        completeTimer = setTimeout(function () { b.classList.remove('complete'); completeTimer = null; }, 600);
    }
    window.progressBarStart = start; window.progressBarStop = stop;
    document.addEventListener('htmx:beforeRequest', function () { start(); });
    document.addEventListener('htmx:afterRequest', function () { stop(); });
    document.addEventListener('htmx:responseError', function () { stop(); });
})();

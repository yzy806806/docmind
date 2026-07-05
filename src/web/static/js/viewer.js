/* viewer.js — Document reader controls: font size, line height, in-document
 * search with highlighting, and TOC scroll-spy.
 *
 * Extracted from viewer.html inline <script> block.
 * Loaded via {% block extra_js %} in viewer.html.
 */
(function() {
    "use strict";

    var root = document.documentElement;
    // Reading mode controls
    var fontSlider = document.getElementById('fontSizeSlider');
    var lhSlider = document.getElementById('lineHeightSlider');
    var reader = document.querySelector('.doc-reader');
    function applyFont() {
        if (reader && fontSlider) reader.style.setProperty('--reader-font-size', fontSlider.value + 'px');
    }
    function applyLineHeight() {
        if (reader && lhSlider) reader.style.setProperty('--reader-line-height', lhSlider.value);
    }
    if (fontSlider) { fontSlider.addEventListener('input', applyFont); applyFont(); }
    if (lhSlider) { lhSlider.addEventListener('input', applyLineHeight); applyLineHeight(); }

    // Search-within-document
    var searchInput = document.getElementById('docSearch');
    var matchCount = document.getElementById('matchCount');
    var prevBtn = document.getElementById('searchPrev');
    var nextBtn = document.getElementById('searchNext');
    var readerEl = document.querySelector('.doc-reader');
    var matches = [];
    var currentIdx = -1;

    function clearHighlights() {
        if (!readerEl) return;
        readerEl.querySelectorAll('mark.search-hit').forEach(function(m) {
            var parent = m.parentNode;
            parent.replaceChild(document.createTextNode(m.textContent), m);
            parent.normalize();
        });
        matches = [];
        currentIdx = -1;
    }

    function highlightTerm(term) {
        if (!readerEl || !term || term.length < 2) { clearHighlights(); updateCount(); return; }
        clearHighlights();
        var walker = document.createTreeWalker(readerEl, NodeFilter.SHOW_TEXT, null);
        var nodes = [];
        var n;
        while ((n = walker.nextNode())) {
            // Skip nodes inside our own mark elements
            if (n.parentNode && n.parentNode.nodeName === 'MARK') continue;
            if (n.textContent.toLowerCase().indexOf(term.toLowerCase()) !== -1) nodes.push(n);
        }
        var lower = term.toLowerCase();
        var tlen = term.length;
        nodes.forEach(function(node) {
            var text = node.textContent;
            var lowerText = text.toLowerCase();
            var idx = 0;
            var frag = document.createDocumentFragment();
            var pos;
            while ((pos = lowerText.indexOf(lower, idx)) !== -1) {
                if (pos > idx) frag.appendChild(document.createTextNode(text.slice(idx, pos)));
                var mark = document.createElement('mark');
                mark.className = 'search-hit';
                mark.textContent = text.slice(pos, pos + tlen);
                frag.appendChild(mark);
                matches.push(mark);
                idx = pos + tlen;
            }
            if (idx < text.length) frag.appendChild(document.createTextNode(text.slice(idx)));
            node.parentNode.replaceChild(frag, node);
        });
        if (matches.length > 0) { currentIdx = 0; scrollToMatch(0); }
        updateCount();
    }

    function updateCount() {
        if (!matchCount) return;
        if (matches.length === 0) {
            matchCount.textContent = searchInput && searchInput.value ? '0 / 0' : '';
        } else {
            matchCount.textContent = (currentIdx + 1) + ' / ' + matches.length;
        }
        if (prevBtn) prevBtn.disabled = matches.length < 2;
        if (nextBtn) nextBtn.disabled = matches.length < 2;
    }

    function scrollToMatch(idx) {
        if (idx < 0 || idx >= matches.length) return;
        if (matches[currentIdx]) matches[currentIdx].classList.remove('current');
        currentIdx = idx;
        var m = matches[currentIdx];
        m.classList.add('current');
        m.scrollIntoView({ behavior: 'smooth', block: 'center' });
        updateCount();
    }

    var debounceTimer;
    if (searchInput) {
        searchInput.addEventListener('input', function() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function() { highlightTerm(searchInput.value.trim()); }, 220);
        });
    }
    if (prevBtn) prevBtn.addEventListener('click', function() {
        if (matches.length === 0) return;
        scrollToMatch((currentIdx - 1 + matches.length) % matches.length);
    });
    if (nextBtn) nextBtn.addEventListener('click', function() {
        if (matches.length === 0) return;
        scrollToMatch((currentIdx + 1) % matches.length);
    });
    if (searchInput) searchInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (e.shiftKey) { if (prevBtn) prevBtn.click(); } else { if (nextBtn) nextBtn.click(); }
        }
    });

    // TOC scroll-spy
    var tocLinks = document.querySelectorAll('.toc-list a[data-anchor]');
    if (tocLinks.length && 'IntersectionObserver' in window) {
        var headings = [];
        tocLinks.forEach(function(link) {
            var target = document.getElementById(link.getAttribute('data-anchor'));
            if (target) headings.push({ link: link, el: target });
        });
        var observer = new IntersectionObserver(function(entries) {
            entries.forEach(function(entry) {
                if (entry.isIntersecting) {
                    tocLinks.forEach(function(l) { l.classList.remove('active'); });
                    var match = headings.find(function(h) { return h.el === entry.target; });
                    if (match) match.link.classList.add('active');
                }
            });
        }, { rootMargin: '-80px 0px -70% 0px' });
        headings.forEach(function(h) { observer.observe(h.el); });
    }
})();

/* lazy-load.js — Infinite scroll and lazy content loading for Phase 9.
 *
 * Provides:
 *   - Infinite scroll for the documents list table: when the sentinel
 *     (#load-more-sentinel) enters the viewport, fetch the next page of
 *     <tr> rows from /documents/partials/rows and append them to #doc-tbody.
 *   - Lazy loading for search results: "Load More" button / scroll sentinel
 *     that fetches additional results from /search/partials/results.
 *
 * Progressive enhancement:
 *   - Without IntersectionObserver, the standard pagination links work.
 *   - Without JS, all content is still rendered server-side (first page).
 *
 * Depends on: no external libraries (vanilla JS). Works alongside HTMX.
 */

(function () {
    "use strict";

    // ── Documents list infinite scroll ────────────────────────────

    function initDocumentsListInfiniteScroll() {
        var sentinel = document.getElementById('load-more-sentinel');
        if (!sentinel) return;
        if (!('IntersectionObserver' in window)) return;

        var loading = false;
        var currentPage = parseInt(sentinel.getAttribute('data-page'), 10) || 1;
        var totalPages = parseInt(sentinel.getAttribute('data-total-pages'), 10) || 1;
        var perPage = parseInt(sentinel.getAttribute('data-per-page'), 10) || 20;

        // Filter params to carry through to the rows endpoint
        var source = sentinel.getAttribute('data-source') || '';
        var tag = sentinel.getAttribute('data-tag') || '';
        var collectionId = sentinel.getAttribute('data-collection-id') || '';
        var dateFrom = sentinel.getAttribute('data-date-from') || '';
        var dateTo = sentinel.getAttribute('data-date-to') || '';
        var fileType = sentinel.getAttribute('data-file-type') || '';

        var tbody = document.getElementById('doc-tbody');
        if (!tbody) return;

        // Insert a loading indicator after the tbody
        var loadingIndicator = document.createElement('div');
        loadingIndicator.id = 'load-more-loading';
        loadingIndicator.style.cssText = 'text-align:center;padding:12px;color:var(--text-muted,#888);font-size:0.85em;display:none;';
        loadingIndicator.textContent = 'Loading more documents…';
        sentinel.parentNode.insertBefore(loadingIndicator, sentinel);

        function buildUrl(page) {
            var params = new URLSearchParams();
            params.set('page', page);
            params.set('per_page', perPage);
            if (source) params.set('source', source);
            if (tag) params.set('tag', tag);
            if (collectionId) params.set('collection_id', collectionId);
            if (dateFrom) params.set('date_from', dateFrom);
            if (dateTo) params.set('date_to', dateTo);
            if (fileType) params.set('file_type', fileType);
            return '/documents/partials/rows?' + params.toString();
        }

        function loadMore() {
            if (loading) return;
            if (currentPage >= totalPages) return;

            loading = true;
            loadingIndicator.style.display = 'block';

            var nextPage = currentPage + 1;
            var url = buildUrl(nextPage);

            fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
                .then(function (response) {
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    // Read pagination metadata from headers
                    var pageHeader = response.headers.get('X-Total-Pages');
                    if (pageHeader) totalPages = parseInt(pageHeader, 10);
                    return response.text();
                })
                .then(function (html) {
                    // Create a temporary container to parse the HTML fragment
                    var temp = document.createElement('tbody');
                    temp.innerHTML = html;
                    while (temp.firstChild) {
                        tbody.appendChild(temp.firstChild);
                    }
                    currentPage = nextPage;
                    // Update the sentinel's data attributes
                    sentinel.setAttribute('data-page', currentPage);

                    // Re-init bulk action button state for newly added rows
                    if (typeof updateBulkActionButtons === 'function') {
                        updateBulkActionButtons();
                    }

                    // If we've loaded all pages, remove the sentinel
                    if (currentPage >= totalPages) {
                        sentinel.style.display = 'none';
                        if (observer) observer.disconnect();
                    }
                })
                .catch(function (err) {
                    // Silent failure — pagination links still work as fallback
                    console.warn('Lazy load failed:', err.message);
                })
                .finally(function () {
                    loading = false;
                    loadingIndicator.style.display = 'none';
                });
        }

        var observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    loadMore();
                }
            });
        }, {
            rootMargin: '200px 0px',  // start loading 200px before sentinel is visible
            threshold: 0
        });

        observer.observe(sentinel);
    }

    // ── Search results lazy loading ───────────────────────────────

    function initSearchResultsLazyLoad() {
        var sentinel = document.getElementById('search-load-more-sentinel');
        if (!sentinel) return;

        var loading = false;
        var query = sentinel.getAttribute('data-query') || '';
        var vectorWeight = sentinel.getAttribute('data-vector-weight') || '';
        var offset = parseInt(sentinel.getAttribute('data-offset'), 10) || 0;
        var total = parseInt(sentinel.getAttribute('data-total'), 10) || 0;
        var limit = parseInt(sentinel.getAttribute('data-limit'), 10) || 20;

        var resultsContainer = document.getElementById('search-results-list');
        if (!resultsContainer) return;

        var loadingIndicator = document.getElementById('search-load-more-loading');
        var loadMoreBtn = document.getElementById('search-load-more-btn');

        function buildUrl(newOffset) {
            var params = new URLSearchParams();
            params.set('q', query);
            if (vectorWeight) params.set('vector_weight', vectorWeight);
            params.set('offset', newOffset);
            params.set('limit', limit);
            params.set('partial', '1');
            return '/search?' + params.toString();
        }

        function loadMore() {
            if (loading) return;
            if (offset >= total) return;

            loading = true;
            if (loadingIndicator) loadingIndicator.style.display = 'block';
            if (loadMoreBtn) loadMoreBtn.disabled = true;

            fetch(buildUrl(offset), { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
                .then(function (response) {
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return response.text();
                })
                .then(function (html) {
                    var temp = document.createElement('div');
                    temp.innerHTML = html;
                    while (temp.firstChild) {
                        resultsContainer.appendChild(temp.firstChild);
                    }
                    offset += limit;
                    sentinel.setAttribute('data-offset', offset);

                    if (offset >= total) {
                        // All results loaded — hide the sentinel and button
                        sentinel.style.display = 'none';
                        if (loadMoreBtn) loadMoreBtn.style.display = 'none';
                        if (loadingIndicator) loadingIndicator.style.display = 'none';
                        if (observer) observer.disconnect();
                    }
                })
                .catch(function (err) {
                    console.warn('Search lazy load failed:', err.message);
                })
                .finally(function () {
                    loading = false;
                    if (loadingIndicator) loadingIndicator.style.display = 'none';
                    if (loadMoreBtn) loadMoreBtn.disabled = false;
                });
        }

        // Use IntersectionObserver if available, otherwise use button
        var observer = null;
        if ('IntersectionObserver' in window) {
            observer = new IntersectionObserver(function (entries) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        loadMore();
                    }
                });
            }, {
                rootMargin: '200px 0px',
                threshold: 0
            });
            observer.observe(sentinel);
        }

        // Also wire up the manual "Load More" button as fallback
        if (loadMoreBtn) {
            loadMoreBtn.addEventListener('click', function (e) {
                e.preventDefault();
                loadMore();
            });
        }
    }

    // ── Lazy-load document detail content preview ─────────────────

    function initDocumentDetailLazyPreview() {
        var previewContainer = document.getElementById('doc-excerpt-lazy');
        if (!previewContainer) return;
        if (!('IntersectionObserver' in window)) return;

        var docId = previewContainer.getAttribute('data-doc-id');
        if (!docId) return;

        var loaded = false;
        var observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting && !loaded) {
                    loaded = true;
                    // Show a loading placeholder
                    previewContainer.innerHTML = '<em style="color:var(--text-muted,#888);">Loading preview…</em>';

                    fetch('/documents/' + docId + '/partials/excerpt', {
                        headers: { 'X-Requested-With': 'XMLHttpRequest' }
                    })
                        .then(function (response) {
                            if (!response.ok) throw new Error('HTTP ' + response.status);
                            return response.text();
                        })
                        .then(function (html) {
                            previewContainer.innerHTML = html;
                        })
                        .catch(function (err) {
                            previewContainer.innerHTML = '<em>Preview unavailable.</em>';
                            console.warn('Excerpt lazy load failed:', err.message);
                        })
                        .finally(function () {
                            observer.disconnect();
                        });
                }
            });
        }, { rootMargin: '100px 0px', threshold: 0 });

        observer.observe(previewContainer);
    }

    // ── Initialize on DOMContentLoaded ────────────────────────────

    document.addEventListener('DOMContentLoaded', function () {
        initDocumentsListInfiniteScroll();
        initSearchResultsLazyLoad();
        initDocumentDetailLazyPreview();
    });
})();

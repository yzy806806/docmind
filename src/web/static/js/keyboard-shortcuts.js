/**
 * DocMind — Keyboard Shortcuts Module (Phase 9)
 *
 * Provides Gmail-style keyboard shortcuts for common actions:
 *
 * Navigation (g followed by a key):
 *   g d  → Dashboard       g s  → Search         g D  → Documents
 *   g u  → Upload          g e  → Email          g j  → Jobs
 *   g a  → Analytics       g c  → Chat           g x  → Settings
 *
 * Quick actions:
 *   /    → Focus search input
 *   ?    → Toggle shortcuts help modal
 *   Esc  → Close modal / blur focused input
 *
 * Document operations (on documents list page):
 *   e    → Focus bulk export select
 *   t    → Focus bulk tag input
 *   m    → Focus bulk move select
 *   Del  → Trigger bulk delete (with confirmation)
 *
 * Design notes:
 * - Uses a single keydown listener on document.
 * - Shortcuts are suppressed when focus is in an input, textarea, select,
 *   or contentEditable element (except Escape, which always works).
 * - The "g" prefix uses a 700ms timeout to cancel if no follow-up key.
 * - Follows the islands convention: vanilla JS, no dependencies, IIFE.
 */

(function () {
    "use strict";

    var G_PREFIX_TIMEOUT = 700; // ms to wait for second key after "g"

    // Map of nav shortcut second-keys to URLs.
    var NAV_TARGETS = {
        "d": "/",            // Dashboard
        "s": "/search",      // Search
        "D": "/documents",   // Documents (capital D)
        "u": "/upload",      // Upload
        "e": "/email-accounts", // Email
        "j": "/jobs",        // Jobs
        "a": "/analytics",   // Analytics
        "c": "/chat",        // Chat
        "x": "/settings"     // Settings
    };

    // ── Helpers ──────────────────────────────────────────────

    /**
     * Check if the current focus is inside an editable element.
     * Shortcuts (except Escape) should be suppressed in this case.
     */
    function isEditable() {
        var el = document.activeElement;
        if (!el) return false;
        var tag = el.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
            return true;
        }
        if (el.isContentEditable) return true;
        return false;
    }

    /**
     * Navigate to a URL.
     */
    function go(url) {
        window.location.href = url;
    }

    /**
     * Focus the first visible search input on the page.
     * Looks for input[name="q"] (used in search, dashboard, search results).
     */
    function focusSearch() {
        var searchInput = document.querySelector('input[name="q"]');
        if (searchInput) {
            searchInput.focus();
            // Select all text if there's existing content
            if (searchInput.select) searchInput.select();
            return true;
        }
        return false;
    }

    /**
     * Toggle the keyboard shortcuts help modal.
     */
    function toggleHelpModal() {
        var modal = document.getElementById("kbd-shortcuts-modal");
        if (modal) {
            modal.classList.toggle("open");
            return true;
        }
        return false;
    }

    /**
     * Close the help modal if it's open.
     */
    function closeHelpModal() {
        var modal = document.getElementById("kbd-shortcuts-modal");
        if (modal) {
            modal.classList.remove("open");
            return true;
        }
        return false;
    }

    /**
     * Focus a bulk action element on the documents list page.
     * @param {string} id - Element ID to focus
     */
    function focusBulkElement(id) {
        var el = document.getElementById(id);
        if (el && !el.disabled) {
            el.focus();
            return true;
        }
        return false;
    }

    /**
     * Trigger bulk delete on the documents list page.
     * Calls the existing confirmBulkDelete() function if available.
     */
    function triggerBulkDelete() {
        var form = document.getElementById("bulk-delete-form");
        if (form && typeof confirmBulkDelete === "function") {
            if (confirmBulkDelete()) {
                form.submit();
                return true;
            }
        }
        return false;
    }

    // ── State ────────────────────────────────────────────────

    var gPrefixActive = false;
    var gPrefixTimer = null;

    function clearGPrefix() {
        gPrefixActive = false;
        if (gPrefixTimer) {
            clearTimeout(gPrefixTimer);
            gPrefixTimer = null;
        }
    }

    // ── Main keydown handler ─────────────────────────────────

    document.addEventListener("keydown", function (e) {
        var key = e.key;

        // Escape always works — close modal or blur focused element
        if (key === "Escape") {
            if (closeHelpModal()) {
                e.preventDefault();
                return;
            }
            // Blur the active element to exit edit mode
            if (isEditable()) {
                document.activeElement.blur();
                e.preventDefault();
                return;
            }
            clearGPrefix();
            return;
        }

        // If we're in an editable element, don't intercept shortcuts
        // (except Escape, handled above)
        if (isEditable()) {
            clearGPrefix();
            return;
        }

        // "g" prefix for navigation shortcuts
        if (key === "g" && !gPrefixActive) {
            gPrefixActive = true;
            gPrefixTimer = setTimeout(clearGPrefix, G_PREFIX_TIMEOUT);
            e.preventDefault();
            return;
        }

        // If "g" prefix is active, check for navigation target
        if (gPrefixActive) {
            clearGPrefix();
            if (NAV_TARGETS.hasOwnProperty(key)) {
                go(NAV_TARGETS[key]);
                e.preventDefault();
                return;
            }
            // Unknown second key after "g" — just cancel
            return;
        }

        // "/" — focus search
        if (key === "/" || (key === "s" && !gPrefixActive)) {
            // Only intercept bare "/" as search focus
            if (key === "/") {
                if (focusSearch()) {
                    e.preventDefault();
                    return;
                }
            }
        }

        // "?" — toggle help modal
        if (key === "?" || (e.shiftKey && key === "/")) {
            if (toggleHelpModal()) {
                e.preventDefault();
                return;
            }
        }

        // Document operations — only on pages with bulk actions
        if (document.getElementById("bulk-actions-bar")) {
            switch (key) {
                case "e":
                    if (focusBulkElement("bulk-export-format")) {
                        e.preventDefault();
                    }
                    return;
                case "t":
                    if (focusBulkElement("bulk-tag-input")) {
                        e.preventDefault();
                    }
                    return;
                case "m":
                    if (focusBulkElement("bulk-move-select")) {
                        e.preventDefault();
                    }
                    return;
                case "Delete":
                    if (triggerBulkDelete()) {
                        e.preventDefault();
                    }
                    return;
            }
        }
    });

    // Close modal when clicking outside it
    document.addEventListener("click", function (e) {
        var modal = document.getElementById("kbd-shortcuts-modal");
        if (modal && e.target === modal) {
            modal.classList.remove("open");
        }
    });

    // ── Build help modal dynamically (if not already in template) ──

    function buildHelpModal() {
        if (document.getElementById("kbd-shortcuts-modal")) return;

        var overlay = document.createElement("div");
        overlay.id = "kbd-shortcuts-modal";
        overlay.className = "kbd-modal-overlay";
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", "Keyboard shortcuts");

        var panel = document.createElement("div");
        panel.className = "kbd-modal-panel";

        var html = "";
        html += '<div class="kbd-modal-header">';
        html += '<h2>⌨️ Keyboard Shortcuts</h2>';
        html += '<button class="kbd-modal-close" onclick="document.getElementById(\'kbd-shortcuts-modal\').classList.remove(\'open\')" aria-label="Close">✕</button>';
        html += '</div>';

        html += '<div class="kbd-modal-body">';
        html += '<div class="kbd-section">';
        html += '<h3>Navigation <span class="kbd-hint">Press g then a key</span></h3>';
        html += '<table class="kbd-table">';
        html += '<tr><td><kbd>g</kbd> <kbd>d</kbd></td><td>Dashboard</td></tr>';
        html += '<tr><td><kbd>g</kbd> <kbd>s</kbd></td><td>Search</td></tr>';
        html += '<tr><td><kbd>g</kbd> <kbd>D</kbd></td><td>Documents</td></tr>';
        html += '<tr><td><kbd>g</kbd> <kbd>u</kbd></td><td>Upload</td></tr>';
        html += '<tr><td><kbd>g</kbd> <kbd>e</kbd></td><td>Email</td></tr>';
        html += '<tr><td><kbd>g</kbd> <kbd>j</kbd></td><td>Jobs</td></tr>';
        html += '<tr><td><kbd>g</kbd> <kbd>a</kbd></td><td>Analytics</td></tr>';
        html += '<tr><td><kbd>g</kbd> <kbd>c</kbd></td><td>Chat</td></tr>';
        html += '<tr><td><kbd>g</kbd> <kbd>x</kbd></td><td>Settings</td></tr>';
        html += '</table>';
        html += '</div>';

        html += '<div class="kbd-section">';
        html += '<h3>Quick Actions</h3>';
        html += '<table class="kbd-table">';
        html += '<tr><td><kbd>/</kbd></td><td>Focus search box</td></tr>';
        html += '<tr><td><kbd>?</kbd></td><td>Show/hide this help</td></tr>';
        html += '<tr><td><kbd>Esc</kbd></td><td>Close modal / blur input</td></tr>';
        html += '</table>';
        html += '</div>';

        html += '<div class="kbd-section">';
        html += '<h3>Document Operations <span class="kbd-hint">On documents list</span></h3>';
        html += '<table class="kbd-table">';
        html += '<tr><td><kbd>e</kbd></td><td>Export selected</td></tr>';
        html += '<tr><td><kbd>t</kbd></td><td>Tag selected</td></tr>';
        html += '<tr><td><kbd>m</kbd></td><td>Move selected</td></tr>';
        html += '<tr><td><kbd>Del</kbd></td><td>Delete selected</td></tr>';
        html += '</table>';
        html += '</div>';
        html += '</div>';

        panel.innerHTML = html;
        overlay.appendChild(panel);
        document.body.appendChild(overlay);
    }

    // Build the modal on DOMContentLoaded (or immediately if already loaded)
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", buildHelpModal);
    } else {
        buildHelpModal();
    }

    // Expose for testing
    window.DocMindKbd = {
        isEditable: isEditable,
        focusSearch: focusSearch,
        toggleHelpModal: toggleHelpModal,
        closeHelpModal: closeHelpModal,
        NAV_TARGETS: NAV_TARGETS,
        buildHelpModal: buildHelpModal
    };
})();

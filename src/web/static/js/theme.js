/**
 * DocMind — Theme Toggle Module
 *
 * Shared theme-toggle logic extracted from base.html and login.html.
 * Persists the user's light/dark preference in localStorage under
 * the key 'docmind-theme' and updates the toggle button icon.
 *
 * This is the first shared JS module in the DocMind "islands"
 * convention: small, self-contained vanilla-JS files served from
 * /static/js/ that pages pull in via <script src>.
 *
 * Usage (in any HTML page):
 *   <script src="/static/js/theme.js" defer></script>
 *   <button class="theme-toggle" onclick="toggleTheme()">🌙</button>
 *
 * The IIFE at the bottom runs on load and applies the stored theme
 * immediately, preventing a flash of the wrong theme (FOUC).
 *
 * Phase 6b (Task 8.10): Added prefers-color-scheme detection — on
 * first visit (no stored preference), the system dark/light mode
 * preference is detected and applied automatically.
 */

(function () {
    "use strict";

    var STORAGE_KEY = "docmind-theme";

    /**
     * Read the saved theme from localStorage.
     * Returns null if no preference has been stored (first visit).
     * @returns {?string} 'light', 'dark', or null if unset
     */
    function getStoredTheme() {
        return localStorage.getItem(STORAGE_KEY);
    }

    /**
     * Detect the system color scheme preference via matchMedia.
     * @returns {string} 'light' or 'dark'
     */
    function getSystemTheme() {
        if (window.matchMedia &&
            window.matchMedia("(prefers-color-scheme: dark)").matches) {
            return "dark";
        }
        return "light";
    }

    /**
     * Get the effective theme: stored preference, or system preference
     * if no manual preference has been set yet.
     * @returns {string} 'light' or 'dark'
     */
    function getEffectiveTheme() {
        var stored = getStoredTheme();
        if (stored) return stored;
        return getSystemTheme();
    }

    /**
     * Apply the given theme to the document root element.
     * @param {string} theme - 'light' or 'dark'
     */
    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        updateToggleIcon(theme);
    }

    /**
     * Toggle between light and dark themes, persisting the new
     * value to localStorage. Called by onclick handlers on
     * .theme-toggle buttons.
     */
    window.toggleTheme = function () {
        var current = document.documentElement.getAttribute("data-theme") || "light";
        var next = current === "dark" ? "light" : "dark";
        applyTheme(next);
        localStorage.setItem(STORAGE_KEY, next);
    };

    /**
     * Update the icon text of the theme-toggle button to match
     * the current theme (🌙 for light, ☀️ for dark).
     * @param {string} theme - 'light' or 'dark'
     */
    function updateToggleIcon(theme) {
        var btn = document.querySelector(".theme-toggle");
        if (btn) btn.textContent = theme === "dark" ? "☀️" : "🌙";
    }

    // Expose for unit-test assertions if needed
    window.DocMindTheme = {
        getStoredTheme: getStoredTheme,
        getSystemTheme: getSystemTheme,
        getEffectiveTheme: getEffectiveTheme,
        applyTheme: applyTheme,
        updateToggleIcon: updateToggleIcon,
        STORAGE_KEY: STORAGE_KEY
    };

    // Apply effective theme on script load (runs immediately, before
    // DOMContentLoaded, to prevent a flash of unstyled content).
    // Uses stored preference if available, otherwise falls back to
    // the system's prefers-color-scheme setting (Task 8.10).
    applyTheme(getEffectiveTheme());

    // Listen for system theme changes — only update if the user
    // hasn't set a manual preference (no stored value).
    if (window.matchMedia) {
        var mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
        if (mediaQuery.addEventListener) {
            mediaQuery.addEventListener("change", onSystemThemeChange);
        } else if (mediaQuery.addListener) {
            // Safari < 14 fallback
            mediaQuery.addListener(onSystemThemeChange);
        }
    }

    function onSystemThemeChange() {
        if (!getStoredTheme()) {
            applyTheme(getSystemTheme());
        }
    }
})();

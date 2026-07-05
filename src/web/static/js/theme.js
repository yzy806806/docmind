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
 */

(function () {
    "use strict";

    /**
     * Read the saved theme from localStorage, defaulting to 'light'.
     * @returns {string} 'light' or 'dark'
     */
    function getStoredTheme() {
        return localStorage.getItem("docmind-theme") || "light";
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
        localStorage.setItem("docmind-theme", next);
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
        applyTheme: applyTheme,
        updateToggleIcon: updateToggleIcon,
    };

    // Apply stored theme on script load (runs immediately, before
    // DOMContentLoaded, to prevent a flash of unstyled content).
    applyTheme(getStoredTheme());
})();

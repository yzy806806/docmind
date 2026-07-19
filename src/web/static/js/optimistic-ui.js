/* optimistic-ui.js — Optimistic UI for HTMX / form mutation actions.
 *
 * Implements the optimistic UI pattern for document mutations:
 *   - Delete (single + bulk): rows fade out immediately on click
 *   - Tag add (single + bulk): new tag badge appears instantly
 *   - Tag remove: tag badge fades out instantly
 *   - Move/assign collection: collection display updates instantly
 *
 * Works by intercepting submit events on forms marked with
 * `data-optimistic` (and optionally `data-optimistic-action`).
 *
 * Progressive enhancement: if JS is absent, forms submit normally.
 */
(function () {
    "use strict";

    var toastContainer = null;
    var toastTimer = null;

    function getToastContainer() {
        if (!toastContainer) {
            toastContainer = document.getElementById('optimistic-toast');
        }
        return toastContainer;
    }

    function showToast(message, type) {
        var container = getToastContainer();
        if (!container) return;
        var toast = document.createElement('div');
        toast.className = 'optimistic-toast-msg optimistic-toast-' + (type || 'info');
        toast.textContent = message;
        while (container.firstChild) container.removeChild(container.firstChild);
        container.appendChild(toast);
        container.classList.add('show');
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(function () {
            container.classList.remove('show');
            setTimeout(function () {
                while (container.firstChild) container.removeChild(container.firstChild);
            }, 300);
        }, 3000);
    }

    function snapshotElement(el) {
        return {
            el: el, parent: el.parentNode, nextSibling: el.nextSibling,
            display: el.style.display, opacity: el.style.opacity,
            classList: el.className, innerHTML: el.innerHTML
        };
    }

    function restoreSnapshot(snap) {
        if (!snap || !snap.parent) return;
        if (!snap.parent.contains(snap.el)) {
            if (snap.nextSibling && snap.nextSibling.parentNode === snap.parent) {
                snap.parent.insertBefore(snap.el, snap.nextSibling);
            } else {
                snap.parent.appendChild(snap.el);
            }
        }
        snap.el.style.display = snap.display;
        snap.el.style.opacity = snap.opacity;
        snap.el.className = snap.classList;
        snap.el.innerHTML = snap.innerHTML;
    }

    function setButtonLoading(btn, label) {
        if (!btn) return;
        btn.dataset.optimisticDisabled = btn.disabled ? '1' : '0';
        btn.dataset.optimisticHtml = btn.innerHTML;
        btn.disabled = true;
        btn.classList.add('optimistic-btn-loading');
        btn.innerHTML = '<span class="optimistic-spinner"></span>' + (label || '…');
    }

    function restoreButton(btn) {
        if (!btn || !btn.dataset.optimisticHtml) return;
        btn.disabled = btn.dataset.optimisticDisabled === '1';
        btn.classList.remove('optimistic-btn-loading');
        btn.innerHTML = btn.dataset.optimisticHtml;
        delete btn.dataset.optimisticHtml;
        delete btn.dataset.optimisticDisabled;
    }

    /* flashElement — applies a transient flash animation to an element to
     * give the user visual confirmation that a mutation succeeded or
     * visual feedback that it failed and was rolled back.
     */
    var FLASH_DURATION = 900;
    function flashElement(el, type) {
        if (!el) return;
        var cls = type === 'error' ? 'optimistic-flash-error' : 'optimistic-flash-success';
        el.classList.remove(cls);
        void el.offsetWidth;
        el.classList.add(cls);
        if (el._optimisticFlashTimer) clearTimeout(el._optimisticFlashTimer);
        el._optimisticFlashTimer = setTimeout(function () {
            el.classList.remove(cls);
            el._optimisticFlashTimer = null;
        }, FLASH_DURATION + 50);
    }

    /* extractErrorMessage — parses a fetch error response and returns
     * a human-readable message. Returns string for Error, Promise for Response.
     */
    function extractErrorMessage(errorOrResponse, defaultMsg) {
        if (!errorOrResponse) return defaultMsg;
        if (errorOrResponse instanceof Error) {
            return errorOrResponse.message || defaultMsg;
        }
        if (errorOrResponse.text && typeof errorOrResponse.text === 'function') {
            return errorOrResponse.text().then(function (body) {
                return parseErrorBody(body, errorOrResponse.status, defaultMsg);
            });
        }
        return defaultMsg;
    }

    function parseErrorBody(body, status, defaultMsg) {
        if (!body) return defaultMsg;
        try {
            var json = JSON.parse(body);
            if (json.detail) return json.detail;
            if (json.message) return json.message;
            if (json.error) return json.error;
        } catch (e) { }
        var msgMatch = body.match(/<main[^>]*>([\s\S]*?)<\/main>/i);
        if (!msgMatch) msgMatch = body.match(/<p[^>]*>([\s\S]*?)<\/p>/i);
        if (msgMatch) {
            var text = msgMatch[1].replace(/<[^>]+>/g, '').trim();
            if (text && text.length < 300) return text;
        }
        if (status && status >= 400) return defaultMsg + ' (HTTP ' + status + ')';
        return defaultMsg;
    }

    function handleSingleDelete(form, formData) {
        var card = document.querySelector('.card.doc-detail');
        var btn = form.querySelector('button[type="submit"]');
        var snapshots = [];
        if (card) {
            snapshots.push(snapshotElement(card));
            card.classList.add('optimistic-removing');
        }
        setButtonLoading(btn, 'Deleting…');
        return {
            snapshots: snapshots, btn: btn,
            onSuccess: function () {
                if (card) flashElement(card, 'success');
                window.location.href = '/documents';
            },
            onError: function (msg) {
                if (card) {
                    card.classList.remove('optimistic-removing');
                    flashElement(card, 'error');
                }
                restoreButton(btn);
                showToast(msg || 'Failed to delete document. Please try again.', 'error');
            }
        };
    }

    function handleBulkDelete(form, formData) {
        var checkboxes = document.querySelectorAll('.doc-checkbox:checked');
        var btn = document.getElementById('delete-selected-btn');
        var snapshots = [], rows = [];
        for (var i = 0; i < checkboxes.length; i++) {
            var row = checkboxes[i].closest('tr');
            if (row) {
                snapshots.push(snapshotElement(row));
                row.classList.add('optimistic-removing');
                rows.push(row);
            }
        }
        setButtonLoading(btn, 'Deleting ' + checkboxes.length + '…');
        return {
            snapshots: snapshots, btn: btn,
            onSuccess: function () {
                for (var i = 0; i < rows.length; i++) {
                    if (rows[i].parentNode) rows[i].parentNode.removeChild(rows[i]);
                }
                if (typeof updateBulkActionButtons === 'function') updateBulkActionButtons();
                showToast('Deleted ' + rows.length + ' document(s).', 'success');
            },
            onError: function (msg) {
                for (var i = 0; i < snapshots.length; i++) {
                    restoreSnapshot(snapshots[i]);
                    snapshots[i].el.classList.remove('optimistic-removing');
                    flashElement(snapshots[i].el, 'error');
                }
                restoreButton(btn);
                showToast(msg || 'Failed to delete documents. Please try again.', 'error');
            }
        };
    }

    function handleSingleTagAdd(form, formData) {
        var tagInput = form.querySelector('input[name="tag"]');
        var tagValue = tagInput ? tagInput.value.trim() : '';
        var tagsContainer = document.querySelector('.doc-tags-section .doc-tags');
        var btn = form.querySelector('button[type="submit"]');
        if (!tagValue) return null;
        var badge = null;
        if (tagsContainer) {
            badge = document.createElement('span');
            badge.className = 'tag-pill optimistic-added';
            badge.textContent = tagValue;
            tagsContainer.appendChild(badge);
        } else {
            var field = document.querySelector('.doc-tags-section .field');
            if (field) {
                var noTags = field.querySelector('em');
                if (noTags && /no tags/i.test(noTags.textContent)) noTags.style.display = 'none';
                badge = document.createElement('span');
                badge.className = 'tag-pill optimistic-added';
                badge.textContent = tagValue;
                field.appendChild(badge);
            }
        }
        setButtonLoading(btn, 'Adding…');
        tagInput.value = '';
        return {
            snapshots: badge ? [{el: badge, parent: badge.parentNode, nextSibling: badge.nextSibling}] : [],
            btn: btn, tagInput: tagInput,
            onSuccess: function () {
                if (badge) {
                    badge.classList.remove('optimistic-added');
                    flashElement(badge, 'success');
                }
                restoreButton(btn);
                showToast('Tag "' + tagValue + '" added.', 'success');
            },
            onError: function (msg) {
                if (badge && badge.parentNode) badge.parentNode.removeChild(badge);
                if (tagInput) tagInput.value = tagValue;
                restoreButton(btn);
                showToast(msg || 'Failed to add tag. Please try again.', 'error');
            }
        };
    }

    function handleTagRemove(form, formData) {
        var action = form.getAttribute('action') || '';
        var tagMatch = action.match(/\/tags\/([^/]+)\/delete/);
        var tagValue = tagMatch ? decodeURIComponent(tagMatch[1]) : '';
        var btn = form.querySelector('button[type="submit"]');
        var badge = form.closest('.tag-pill');
        if (!badge) {
            var badges = document.querySelectorAll('.doc-tags-section .tag-pill, .doc-tags-section .badge, .doc-tags-section .tag-badge');
            for (var i = 0; i < badges.length; i++) {
                if (badges[i].textContent.replace('✕', '').trim() === tagValue) { badge = badges[i]; break; }
            }
        }
        if (badge) badge.classList.add('optimistic-removing');
        setButtonLoading(btn, 'Removing…');
        return {
            snapshots: badge ? [snapshotElement(badge)] : [], btn: btn,
            onSuccess: function () {
                if (badge && badge.parentNode) {
                    flashElement(badge, 'success');
                    if (badge.parentNode) badge.parentNode.removeChild(badge);
                }
                restoreButton(btn);
                showToast('Tag "' + tagValue + '" removed.', 'success');
            },
            onError: function (msg) {
                if (badge) {
                    badge.classList.remove('optimistic-removing');
                    flashElement(badge, 'error');
                }
                restoreButton(btn);
                showToast(msg || 'Failed to remove tag. Please try again.', 'error');
            }
        };
    }

    function handleBulkTag(form, formData) {
        var tagInput = document.getElementById('bulk-tag-input');
        var tagValue = tagInput ? tagInput.value.trim() : '';
        var btn = document.getElementById('tag-selected-btn');
        var checkboxes = document.querySelectorAll('.doc-checkbox:checked');
        var snapshots = [];
        if (!tagValue) return null;
        for (var i = 0; i < checkboxes.length; i++) {
            var row = checkboxes[i].closest('tr');
            if (!row) continue;
            var tagCell = row.cells[row.cells.length - 2];
            if (tagCell) {
                var badge = document.createElement('span');
                badge.className = 'tag-pill optimistic-added';
                badge.textContent = tagValue;
                tagCell.appendChild(badge);
                snapshots.push({el: badge, parent: tagCell});
            }
        }
        setButtonLoading(btn, 'Tagging ' + checkboxes.length + '…');
        return {
            snapshots: snapshots, btn: btn,
            onSuccess: function () {
                for (var i = 0; i < snapshots.length; i++) {
                    if (snapshots[i].el) {
                        snapshots[i].el.classList.remove('optimistic-added');
                        flashElement(snapshots[i].el, 'success');
                    }
                }
                restoreButton(btn);
                if (tagInput) tagInput.value = '';
                showToast('Tagged ' + checkboxes.length + ' document(s) with "' + tagValue + '".', 'success');
            },
            onError: function (msg) {
                for (var i = 0; i < snapshots.length; i++) {
                    if (snapshots[i].el && snapshots[i].el.parentNode) snapshots[i].el.parentNode.removeChild(snapshots[i].el);
                }
                restoreButton(btn);
                showToast(msg || 'Failed to tag documents. Please try again.', 'error');
            }
        };
    }

    function handleBulkMove(form, formData) {
        var selectEl = document.getElementById('bulk-move-select');
        var btn = document.getElementById('move-selected-btn');
        var colName = selectEl ? selectEl.options[selectEl.selectedIndex].textContent : '';
        var checkboxes = document.querySelectorAll('.doc-checkbox:checked');
        setButtonLoading(btn, 'Moving ' + checkboxes.length + '…');
        return {
            snapshots: [], btn: btn,
            onSuccess: function () {
                restoreButton(btn);
                showToast('Moved ' + checkboxes.length + ' document(s) to "' + colName + '".', 'success');
            },
            onError: function (msg) {
                restoreButton(btn);
                showToast(msg || 'Failed to move documents. Please try again.', 'error');
            }
        };
    }

    function handleCollectionAssign(form, formData) {
        var btn = form.querySelector('button[type="submit"]');
        setButtonLoading(btn, 'Assigning…');
        return {
            snapshots: [], btn: btn,
            onSuccess: function (response) {
                if (response.redirected) window.location.href = response.url;
                else window.location.reload();
            },
            onError: function (msg) {
                restoreButton(btn);
                showToast(msg || 'Failed to assign collection. Please try again.', 'error');
            }
        };
    }

    var handlerMap = {
        'single-delete': handleSingleDelete,
        'bulk-delete': handleBulkDelete,
        'single-tag-add': handleSingleTagAdd,
        'tag-remove': handleTagRemove,
        'bulk-tag': handleBulkTag,
        'bulk-move': handleBulkMove,
        'collection-assign': handleCollectionAssign
    };

    function getFormActionType(form) {
        var actionType = form.getAttribute('data-optimistic-action');
        if (actionType) return actionType;
        var action = form.getAttribute('action') || '';
        if (/\/bulk-delete$/.test(action)) return 'bulk-delete';
        if (/\/bulk-tag$/.test(action)) return 'bulk-tag';
        if (/\/bulk-move-collection$/.test(action)) return 'bulk-move';
        if (/\/tags\/[^/]+\/delete$/.test(action)) return 'tag-remove';
        if (/\/tags$/.test(action)) return 'single-tag-add';
        if (/\/assign-collection$/.test(action)) return 'collection-assign';
        if (/\/delete$/.test(action)) return 'single-delete';
        return null;
    }

    function interceptSubmit(event) {
        var form = event.target;
        if (!form || !form.hasAttribute('data-optimistic')) return;
        if (form.hasAttribute('hx-post') || form.hasAttribute('hx-put') ||
            form.hasAttribute('hx-patch') || form.hasAttribute('hx-delete')) return;
        event.preventDefault();
        event.stopPropagation();
        var actionType = getFormActionType(form);
        if (!actionType) return;
        var handler = handlerMap[actionType];
        if (!handler) return;
        var formData = new FormData(form);
        var context;
        try { context = handler(form, formData); }
        catch (e) {
            console.error('Optimistic UI handler error:', e);
            form.removeAttribute('data-optimistic');
            form.submit();
            return;
        }
        if (!context) return;
        fetch(form.action, { method: 'POST', body: formData, redirect: 'follow' })
            .then(function (response) {
                if (response.ok) { if (context.onSuccess) context.onSuccess(response); }
                else {
                    if (context.onError) {
                        extractErrorMessage(response, 'Request failed.').then(function (msg) {
                            context.onError(msg, response);
                        });
                    }
                }
            })
            .catch(function (error) {
                if (context.onError) {
                    var msg = extractErrorMessage(error, 'Network error — please check your connection.');
                    if (msg && msg.then) {
                        msg.then(function (m) { context.onError(m, error); });
                    } else {
                        context.onError(msg, error);
                    }
                }
            });
    }

    function ensureToastContainer() {
        if (!document.getElementById('optimistic-toast')) {
            var container = document.createElement('div');
            container.id = 'optimistic-toast';
            container.className = 'optimistic-toast-container';
            container.setAttribute('aria-live', 'polite');
            container.setAttribute('aria-atomic', 'true');
            document.body.appendChild(container);
            toastContainer = container;
        }
    }

    var _initialised = false;
    function init() {
        if (_initialised) return;
        _initialised = true;
        ensureToastContainer();
        document.addEventListener('submit', interceptSubmit, true);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.OptimisticUI = {
        showToast: showToast, setButtonLoading: setButtonLoading,
        restoreButton: restoreButton, handlers: handlerMap,
        interceptSubmit: interceptSubmit,
        flashElement: flashElement,
        extractErrorMessage: extractErrorMessage
    };
})();

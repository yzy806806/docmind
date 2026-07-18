/* upload.js — Drag-and-drop multi-file upload with per-file progress bars.
 *
 * Extracted from upload_form.html inline <script> block.
 * Loaded via {% block extra_js %} in upload_form.html.
 *
 * Provides a progressive-enhancement layer on top of the plain <form>
 * fallback: if JS is disabled, the basic uploader form is shown instead.
 */
(function() {
    "use strict";

    // Throttle the XHR progress handler — progress events fire many times
    // per second during upload; throttling to 10fps (100ms) avoids layout
    // thrashing from frequent DOM writes (progEl.value + statusEl.textContent).
    // Falls back to a direct handler if perf-utils is absent.
    var _perf = window.DocMindPerf || {};
    var _throttledProgress = _perf.throttle
        ? _perf.throttle(function (pct, el, statusEl) {
            el.value = pct;
            statusEl.textContent = pct + '%';
        }, 100)
        : function (pct, el, statusEl) {
            el.value = pct;
            statusEl.textContent = pct + '%';
        };

    var dropZone   = document.getElementById('drop-zone');
    var fileInput  = document.getElementById('file-input');
    var fileListEl = document.getElementById('file-list');
    var actionsEl  = document.getElementById('upload-actions');
    var uploadBtn  = document.getElementById('upload-btn');
    var clearBtn   = document.getElementById('clear-btn');
    var noJsNote   = document.getElementById('no-js-note');

    // Hide the no-JS notice once JS runs.
    noJsNote.style.display = 'none';

    var selectedFiles = [];

    // ── Drag events ──────────────────────────────────────
    var dragCounter = 0;

    dropZone.addEventListener('dragenter', function(e) {
        e.preventDefault();
        e.stopPropagation();
        dragCounter++;
        dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragover', function(e) {
        e.preventDefault();
        e.stopPropagation();
    });

    dropZone.addEventListener('dragleave', function(e) {
        e.preventDefault();
        e.stopPropagation();
        dragCounter--;
        if (dragCounter === 0) {
            dropZone.classList.remove('drag-over');
        }
    });

    dropZone.addEventListener('drop', function(e) {
        e.preventDefault();
        e.stopPropagation();
        dragCounter = 0;
        dropZone.classList.remove('drag-over');
        var dt = e.dataTransfer;
        if (dt && dt.files) {
            addFiles(dt.files);
        }
    });

    // ── Click to browse ──────────────────────────────────
    dropZone.addEventListener('click', function() {
        fileInput.click();
    });

    fileInput.addEventListener('change', function() {
        if (fileInput.files) {
            addFiles(fileInput.files);
            fileInput.value = '';  // allow re-selecting the same file
        }
    });

    // ── Add files to the list ────────────────────────────
    function addFiles(fileObjs) {
        for (var i = 0; i < fileObjs.length; i++) {
            var f = fileObjs[i];
            // Avoid duplicates by name+size
            var dup = false;
            for (var j = 0; j < selectedFiles.length; j++) {
                if (selectedFiles[j].name === f.name && selectedFiles[j].size === f.size) {
                    dup = true; break;
                }
            }
            if (!dup) {
                selectedFiles.push(f);
                renderFileItem(f, selectedFiles.length - 1);
            }
        }
        updateActions();
    }

    function renderFileItem(f, idx) {
        var row = document.createElement('div');
        row.className = 'file-item';
        row.dataset.idx = idx;
        row.innerHTML =
            '<div class="file-info">' +
                '<span class="file-name"></span>' +
                '<span class="file-size"></span>' +
            '</div>' +
            '<div class="file-progress-wrap">' +
                '<progress class="file-progress" max="100" value="0"></progress>' +
                '<span class="file-status">Queued</span>' +
            '</div>' +
            '<button type="button" class="file-remove" title="Remove">&times;</button>';

        row.querySelector('.file-name').textContent = f.name;
        row.querySelector('.file-size').textContent = formatSize(f.size);

        row.querySelector('.file-remove').addEventListener('click', function() {
            var i = parseInt(row.dataset.idx, 10);
            selectedFiles.splice(i, 1);
            row.remove();
            // Re-index remaining rows
            var rows = fileListEl.querySelectorAll('.file-item');
            rows.forEach(function(r, n) { r.dataset.idx = n; });
            updateActions();
        });

        fileListEl.appendChild(row);
    }

    function formatSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    function updateActions() {
        var n = selectedFiles.length;
        if (n === 0) {
            actionsEl.style.display = 'none';
        } else {
            actionsEl.style.display = 'block';
            uploadBtn.textContent = 'Upload ' + n + ' file(s)';
        }
    }

    // ── Clear ────────────────────────────────────────────
    clearBtn.addEventListener('click', function() {
        selectedFiles = [];
        fileListEl.innerHTML = '';
        updateActions();
    });

    // ── Upload via AJAX ──────────────────────────────────
    uploadBtn.addEventListener('click', function() {
        if (selectedFiles.length === 0) return;
        uploadBtn.disabled = true;
        clearBtn.disabled = true;
        uploadFilesSequential();
    });

    function uploadFilesSequential() {
        // Upload each file individually to /api/v1/documents/submit
        // so we get real per-file progress via XMLHttpRequest.
        var idx = 0;

        function next() {
            if (idx >= selectedFiles.length) {
                // All done → redirect to batch results summary page.
                window.location.href = '/upload?done=1';
                return;
            }
            var f = selectedFiles[idx];
            var row = fileListEl.querySelector('.file-item[data-idx="' + idx + '"]');
            uploadOne(f, row, function(ok) {
                idx++;
                next();
            });
        }
        next();
    }

    function uploadOne(file, row, cb) {
        var statusEl = row.querySelector('.file-status');
        var progEl   = row.querySelector('.file-progress');

        var xhr = new XMLHttpRequest();
        var fd = new FormData();
        fd.append('file', file);

        xhr.open('POST', '/api/v1/documents/submit', true);

        // Progress
        if (xhr.upload) {
            xhr.upload.addEventListener('progress', function(e) {
                if (e.lengthComputable) {
                    var pct = Math.round((e.loaded / e.total) * 100);
                    _throttledProgress(pct, progEl, statusEl);
                }
            });
        }

        xhr.onload = function() {
            if (xhr.status >= 200 && xhr.status < 300) {
                progEl.value = 100;
                statusEl.textContent = '✅ Done';
                row.classList.add('file-done');
            } else {
                var msg = '❌ Failed';
                try {
                    var data = JSON.parse(xhr.responseText);
                    if (data.detail) msg = '❌ ' + data.detail;
                } catch (e) {}
                statusEl.textContent = msg;
                row.classList.add('file-failed');
            }
            cb(true);
        };

        xhr.onerror = function() {
            statusEl.textContent = '❌ Network error';
            row.classList.add('file-failed');
            cb(false);
        };

        xhr.send(fd);
    }
})();

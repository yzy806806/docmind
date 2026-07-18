/* chat.js — Chat WebSocket, session management, export, and citation rendering.
 *
 * Extracted from chat.html inline <script> block.
 * Loaded via {% block extra_js %} in chat.html.
 */
(function() {
    "use strict";

    var ws = null;
    var citations = [];
    var currentAnswer = '';
    var isStreaming = false;
    var sendBtn, inputField;
    var currentSessionId = null;
    var sessionTitle = 'New Chat';

    // rAF-throttled auto-scroll — streaming chunks can fire many times per
    // second; coalescing to one scrollTop write per frame avoids layout
    // thrashing. Falls back to direct scroll if perf-utils is absent.
    var _rafScrollToBottom = (window.DocMindPerf || {}).rAFThrottle
        ? window.DocMindPerf.rAFThrottle(function (box) {
            if (box) box.scrollTop = box.scrollHeight;
        })
        : function (box) { if (box) box.scrollTop = box.scrollHeight; };

    function toggleExportMenu() {
        var menu = document.getElementById('chat-export-menu');
        menu.style.display = (menu.style.display === 'none') ? 'block' : 'none';
    }
    document.addEventListener('click', function(e) {
        var dropdown = document.querySelector('.chat-export-dropdown');
        var menu = document.getElementById('chat-export-menu');
        if (dropdown && menu && !dropdown.contains(e.target)) {
            menu.style.display = 'none';
        }
    });
    function exportChat(format) {
        document.getElementById('chat-export-menu').style.display = 'none';
        if (!currentSessionId) {
            alert('No active conversation to export.');
            return;
        }
        window.location.href = '/api/v1/chat/sessions/' +
            encodeURIComponent(currentSessionId) +
            '/export?format=' + encodeURIComponent(format);
    }

    function getQueryParam(name) {
        var params = new URLSearchParams(window.location.search);
        return params.get(name);
    }

    function getWsUrl() {
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        var url = proto + '//' + location.host + '/chat';
        if (currentSessionId) {
            url += '?session_id=' + encodeURIComponent(currentSessionId);
        }
        return url;
    }

    function setSession(id, title) {
        currentSessionId = id;
        sessionTitle = title || 'New Chat';
        var titleEl = document.getElementById('chat-title');
        if (titleEl) titleEl.textContent = sessionTitle;
        // Update URL without reload
        var newUrl = window.location.pathname;
        if (id) newUrl += '?session=' + encodeURIComponent(id);
        history.replaceState({}, '', newUrl);
    }

    function clearMessages() {
        document.getElementById('chat-messages').innerHTML = '';
        citations = [];
        currentAnswer = '';
        document.getElementById('citations-panel').style.display = 'none';
        document.getElementById('citations-list').innerHTML = '';
    }

    function connectChat() {
        ws = new WebSocket(getWsUrl());
        ws.onopen = function() {
            document.getElementById('chat-status').textContent = 'Connected';
        };
        ws.onclose = function() {
            document.getElementById('chat-status').textContent = 'Disconnected';
            addMsg('bot', 'Disconnected. Reconnecting in 3s...');
            setTimeout(connectChat, 3000);
        };
        ws.onerror = function() {
            document.getElementById('chat-status').textContent = 'Error';
        };
        ws.onmessage = function(event) {
            var msg = JSON.parse(event.data);
            handleChatMessage(msg);
        };
    }
    function sendChat() {
        inputField = document.getElementById('chat-input');
        sendBtn = document.getElementById('chat-send-btn');
        var text = inputField.value.trim();
        if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
        addMsg('user', text);
        ws.send(JSON.stringify({type: 'question', text: text}));
        inputField.value = '';
        inputField.disabled = true;
        sendBtn.disabled = true;
        citations = [];
        currentAnswer = '';
        isStreaming = false;
        document.getElementById('citations-panel').style.display = 'none';
        document.getElementById('citations-list').innerHTML = '';
        showTypingIndicator();
    }
    function showTypingIndicator() {
        var box = document.getElementById('chat-messages');
        var div = document.createElement('div');
        div.className = 'chat-msg bot typing';
        div.id = 'typing-indicator-msg';
        div.innerHTML = 'Thinking<span class="typing-dots"><span></span><span></span><span></span></span>';
        box.appendChild(div);
        _rafScrollToBottom(box);
    }
    function removeTypingIndicator() {
        var el = document.getElementById('typing-indicator-msg');
        if (el) el.remove();
    }
    function handleChatMessage(msg) {
        switch(msg.type) {
            case 'connected':
                if (msg.session_id) {
                    setSession(msg.session_id, msg.title);
                    loadSessionList();
                }
                break;
            case 'history':
                clearMessages();
                if (msg.messages && msg.messages.length) {
                    msg.messages.forEach(function(m) {
                        addMsg(m.role === 'user' ? 'user' : 'bot', m.content);
                        if (m.role === 'assistant' && m.citations && m.citations.length) {
                            citations = m.citations;
                            renderCitations();
                        }
                    });
                }
                break;
            case 'citation:added':
                citations.push(msg);
                renderCitations();
                break;
            case 'answer:chunk':
                removeTypingIndicator();
                appendChunk(msg.text);
                break;
            case 'answer:done':
                removeTypingIndicator();
                if (msg.text && msg.text !== currentAnswer) {
                    var box = document.getElementById('chat-messages');
                    var lastBot = box.querySelector('.chat-msg.bot:last-child');
                    if (lastBot && lastBot.dataset.streaming === 'true') {
                        lastBot.textContent = msg.text;
                        currentAnswer = msg.text;
                    } else {
                        addMsg('bot', msg.text);
                    }
                }
                if (msg.session_id && msg.session_id !== currentSessionId) {
                    setSession(msg.session_id, msg.title);
                }
                isStreaming = false;
                inputField = document.getElementById('chat-input');
                sendBtn = document.getElementById('chat-send-btn');
                inputField.disabled = false;
                sendBtn.disabled = false;
                inputField.focus();
                renderCitations();
                loadSessionList();
                break;
            case 'error':
                removeTypingIndicator();
                addMsg('error', msg.message);
                inputField = document.getElementById('chat-input');
                sendBtn = document.getElementById('chat-send-btn');
                inputField.disabled = false;
                sendBtn.disabled = false;
                break;
            case 'pong':
                break;
        }
    }
    function addMsg(cls, text) {
        var div = document.createElement('div');
        div.className = 'chat-msg ' + cls;
        div.textContent = text;
        document.getElementById('chat-messages').appendChild(div);
        var box = document.getElementById('chat-messages');
        _rafScrollToBottom(box);
    }
    function appendChunk(text) {
        currentAnswer += text;
        var box = document.getElementById('chat-messages');
        var lastBot = box.querySelector('.chat-msg.bot:last-child');
        if (lastBot && lastBot.dataset.streaming === 'true') {
            lastBot.textContent = currentAnswer;
        } else {
            currentAnswer = text;
            addMsg('bot', currentAnswer);
            var last = box.querySelector('.chat-msg.bot:last-child');
            if (last) last.dataset.streaming = 'true';
        }
        _rafScrollToBottom(box);
    }
    function renderCitations() {
        if (citations.length === 0) return;
        var panel = document.getElementById('citations-panel');
        var list = document.getElementById('citations-list');
        list.innerHTML = citations.map(function(c) {
            return '<div class="citation-item"><strong>[' + c.ref + ']</strong> ' +
                   '<a href="/documents/' + c.doc_id + '">' + c.title + '</a>' +
                   ' (confidence: ' + (c.confidence || 'low') + ')</div>';
        }).join('');
        panel.style.display = 'block';
    }
    function loadSessionList() {
        var listEl = document.getElementById('chat-session-list');
        // Show skeleton placeholders while fetching
        if (listEl) {
            var skelHtml = '';
            for (var i = 0; i < 4; i++) {
                skelHtml += '<div class="chat-session-skeleton">' +
                    '<span class="skeleton" style="width:' + (50 + Math.random() * 30) + '%;"></span>' +
                    '<span class="skeleton"></span>' +
                    '</div>';
            }
            listEl.innerHTML = skelHtml;
        }
        fetch('/api/v1/chat/sessions?limit=30')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var listEl = document.getElementById('chat-session-list');
                if (!data.sessions || data.sessions.length === 0) {
                    listEl.innerHTML = '<div class="empty-state" style="padding:var(--space-4);"><span class="empty-icon" style="font-size:1.5em;">💬</span><p class="empty-title" style="font-size:var(--font-size-base);">还没有对话</p><p class="empty-hint">输入问题开始与文档对话</p></div>';
                    return;
                }
                listEl.innerHTML = data.sessions.map(function(s) {
                    var active = (s.id === currentSessionId) ? ' active' : '';
                    var title = s.title || 'New Chat';
                    var preview = s.preview || '';
                    var safeTitle = title.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                    var safePreview = preview.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                    return '<div class="chat-session-item' + active + '" ' +
                           'onclick="loadSession(\'' + s.id + '\', \'' + safeTitle.replace(/'/g, "\\'") + '\')">' +
                           '<div class="chat-session-title">' + safeTitle + '</div>' +
                           '<div class="chat-session-preview">' + safePreview + '</div>' +
                           '<button class="chat-session-del" title="Delete" ' +
                           'onclick="deleteSession(event, \'' + s.id + '\')">&times;</button>' +
                           '</div>';
                }).join('');
            })
            .catch(function() {
                document.getElementById('chat-session-list').innerHTML =
                    '<div class="error-state" style="padding:var(--space-4);"><span class="error-icon" style="font-size:1.5em;">⚠️</span><p class="error-title" style="font-size:var(--font-size-base);">加载失败</p><p class="error-detail">无法加载对话列表，请刷新页面重试</p></div>';
            });
    }
    function loadSession(id, title) {
        setSession(id, title);
        clearMessages();
        // Reconnect WebSocket with the new session_id
        if (ws) { try { ws.close(); } catch(e) {} }
        connectChat();
    }
    function deleteSession(event, id) {
        event.stopPropagation();
        if (!confirm('Delete this conversation? This cannot be undone.')) return;
        fetch('/api/v1/chat/sessions/' + encodeURIComponent(id), {method: 'DELETE'})
            .then(function(r) { if (!r.ok) throw new Error('delete failed'); return r.json(); })
            .then(function() {
                if (id === currentSessionId) {
                    window.location.href = '/chat';
                } else {
                    loadSessionList();
                }
            })
            .catch(function() { alert('Failed to delete session.'); });
    }
    function startNewChat() {
        window.location.href = '/chat';
    }

    // Expose functions needed by inline onclick handlers in the HTML
    window.toggleExportMenu = toggleExportMenu;
    window.exportChat = exportChat;
    window.sendChat = sendChat;
    window.loadSession = loadSession;
    window.deleteSession = deleteSession;

    document.getElementById('new-chat-btn').addEventListener('click', startNewChat);
    // On load: pick up ?session=xxx if present
    (function() {
        var sid = getQueryParam('session');
        if (sid) { currentSessionId = sid; }
        loadSessionList();
        connectChat();
    })();
})();

(function () {
    "use strict";

    const AGENT_KEY = "agentcore.agent";
    const CONVOS_KEY = "agentcore.conversations.v1";
    const ACTIVE_KEY = "agentcore.activeConversation";
    const MAX_CONVERSATIONS = 30;

    const AGENTS = {
        chat: {
            key: "chat",
            label: "Chat",
            letter: "C",
            placeholder: "Ask anything…",
            samples: [
                "Explain the difference between a P1 and P2 incident.",
                "Draft a short status update for an ongoing outage.",
                "What does HTTP 502 usually indicate?",
            ],
        },
        snow: {
            key: "snow",
            label: "SNOW",
            letter: "S",
            placeholder: "Ask about closed incidents…",
            samples: [
                "How was VPN error 809 resolved in past tickets?",
                "Show resolutions for database failover incidents.",
                "What is the standard fix for disk space alerts?",
            ],
        },
        rca: {
            key: "rca",
            label: "RCA",
            letter: "R",
            placeholder: "Describe the new incident to analyze…",
            samples: [
                "New P1: checkout API returns 502 since 09:00 after a deploy.",
                "Payment service latency spiked 10x; users report timeouts.",
                "Nightly batch job fails with ORA-01555 after growing table scans.",
            ],
        },
    };

    const $ = (selector, root) => (root || document).querySelector(selector);
    const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

    const el = {
        sidebar: $("#sidebar"),
        backdrop: $("#backdrop"),
        menuBtn: $("#menuBtn"),
        closeSidebar: $("#closeSidebar"),
        collapseBtn: $("#collapseBtn"),
        expandBtn: $("#expandBtn"),
        newChatBtn: $("#newChatBtn"),
        agentList: $("#agentList"),
        agentGrid: $("#agentGrid"),
        historyList: $("#historyList"),
        clearHistoryBtn: $("#clearHistoryBtn"),
        themeToggle: $("#themeToggle"),
        themeLabel: $("#themeLabel"),
        themeIcon: $("#themeIcon"),
        statusDot: $("#statusDot"),
        statusText: $("#statusText"),
        activeAgentName: $("#activeAgentName"),
        activeAgentChip: $("#activeAgentChip"),
        chat: $("#chat"),
        welcome: $("#welcome"),
        samples: $("#samples"),
        form: $("#chatForm"),
        input: $("#input"),
        sendBtn: $("#sendBtn"),
        composerAgent: $("#composerAgent"),
    };

    const messagesEl = document.createElement("div");
    messagesEl.className = "messages";
    messagesEl.hidden = true;
    el.chat.appendChild(messagesEl);

    const state = {
        agent: safeGet(AGENT_KEY) || "chat",
        conversations: loadConversations(),
        activeId: safeGet(ACTIVE_KEY) || null,
        sending: false,
    };

    if (!AGENTS[state.agent]) state.agent = "chat";

    /* ---------------- storage ---------------- */

    function safeGet(key) {
        try {
            return localStorage.getItem(key);
        } catch (e) {
            return null;
        }
    }

    function safeSet(key, value) {
        try {
            localStorage.setItem(key, value);
        } catch (e) {
            /* storage may be unavailable */
        }
    }

    function safeRemove(key) {
        try {
            localStorage.removeItem(key);
        } catch (e) {
            /* ignore */
        }
    }

    function loadConversations() {
        try {
            const parsed = JSON.parse(safeGet(CONVOS_KEY) || "[]");
            return Array.isArray(parsed) ? parsed : [];
        } catch (e) {
            return [];
        }
    }

    function saveConversations() {
        state.conversations = state.conversations
            .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
            .slice(0, MAX_CONVERSATIONS);

        if (state.activeId && !state.conversations.some((c) => c.id === state.activeId)) {
            state.activeId = null;
        }

        safeSet(CONVOS_KEY, JSON.stringify(state.conversations));
        if (state.activeId) safeSet(ACTIVE_KEY, state.activeId);
        else safeRemove(ACTIVE_KEY);
    }

    function activeConversation() {
        return state.conversations.find((c) => c.id === state.activeId) || null;
    }

    function uid() {
        if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
        return "c-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
    }

    /* ---------------- theme ---------------- */

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        safeSet("agentcore.theme", theme);
        el.themeLabel.textContent = theme === "dark" ? "Dark mode" : "Light mode";
        el.themeToggle.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
    }

    el.themeToggle.addEventListener("click", () => {
        const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
        applyTheme(next);
    });

    /* ---------------- agents ---------------- */

    function setAgent(key, persist) {
        if (!AGENTS[key]) return;
        state.agent = key;
        const meta = AGENTS[key];

        $$(".agent-item").forEach((btn) => {
            const active = btn.dataset.agent === key;
            btn.classList.toggle("active", active);
            btn.setAttribute("aria-checked", active ? "true" : "false");
        });
        $$(".agent-card").forEach((card) => {
            card.classList.toggle("active", card.dataset.agent === key);
        });

        el.activeAgentName.textContent = meta.label;
        el.activeAgentChip.textContent = meta.label.toLowerCase();
        el.composerAgent.textContent = meta.label;
        el.input.placeholder = meta.placeholder;
        renderSamples();

        if (persist !== false) safeSet(AGENT_KEY, key);
    }

    function renderSamples() {
        el.samples.innerHTML = "";
        AGENTS[state.agent].samples.forEach((text) => {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = "sample-chip";
            chip.textContent = text;
            chip.addEventListener("click", () => sendMessage(text));
            el.samples.appendChild(chip);
        });
    }

    el.agentList.addEventListener("click", (event) => {
        const btn = event.target.closest(".agent-item");
        if (btn) setAgent(btn.dataset.agent);
    });

    el.agentGrid.addEventListener("click", (event) => {
        const card = event.target.closest(".agent-card");
        if (card) {
            setAgent(card.dataset.agent);
            el.input.focus();
        }
    });

    /* ---------------- history ---------------- */

    function renderHistory() {
        el.historyList.innerHTML = "";

        if (!state.conversations.length) {
            const empty = document.createElement("div");
            empty.className = "history-empty";
            empty.textContent = "No conversations yet. Start a new chat.";
            el.historyList.appendChild(empty);
            return;
        }

        state.conversations
            .slice()
            .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
            .forEach((convo) => {
                const row = document.createElement("div");
                row.className = "history-item" + (convo.id === state.activeId ? " active" : "");

                const open = document.createElement("button");
                open.type = "button";
                open.className = "history-open";
                open.title = convo.title || "Conversation";
                open.innerHTML = '<span class="history-dot"></span>';
                const title = document.createElement("span");
                title.className = "history-title";
                title.textContent = convo.title || "Untitled";
                open.appendChild(title);
                open.addEventListener("click", () => openConversation(convo.id));

                const del = document.createElement("button");
                del.type = "button";
                del.className = "history-delete";
                del.title = "Delete conversation";
                del.setAttribute("aria-label", "Delete conversation");
                del.innerHTML =
                    '<svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M9 3h6l1 2h4v2H4V5h4zm1 6h2v9h-2zm4 0h2v9h-2zM6 9h12l-1 12H7z"/></svg>';
                del.addEventListener("click", (event) => {
                    event.stopPropagation();
                    deleteConversation(convo.id);
                });

                row.appendChild(open);
                row.appendChild(del);
                el.historyList.appendChild(row);
            });
    }

    function openConversation(id) {
        if (state.sending) return;
        state.activeId = id;
        const convo = activeConversation();
        if (convo) {
            setAgent(convo.agent || state.agent);
            renderConversation();
        }
        renderHistory();
        closeSidebar();
        saveConversations();
    }

    function deleteConversation(id) {
        if (state.sending) return;
        state.conversations = state.conversations.filter((c) => c.id !== id);
        if (state.activeId === id) {
            state.activeId = null;
            renderWelcome();
        }
        saveConversations();
        renderHistory();
    }

    el.clearHistoryBtn.addEventListener("click", () => {
        if (state.sending) return;
        if (!state.conversations.length) return;
        if (!window.confirm("Delete all conversations? This cannot be undone.")) return;
        state.conversations = [];
        state.activeId = null;
        saveConversations();
        renderHistory();
        renderWelcome();
    });

    /* ---------------- rendering ---------------- */

    function renderConversation() {
        const convo = activeConversation();
        if (!convo || !convo.messages.length) {
            renderWelcome();
            return;
        }

        el.welcome.hidden = true;
        messagesEl.hidden = false;
        messagesEl.innerHTML = "";

        convo.messages.forEach((message) => {
            messagesEl.appendChild(buildMessage(message, convo));
        });

        if (state.sending) {
            messagesEl.appendChild(buildTyping(convo.agent || state.agent));
        }

        scrollToBottom();
    }

    function renderWelcome() {
        messagesEl.hidden = true;
        messagesEl.innerHTML = "";
        el.welcome.hidden = false;
        renderSamples();
    }

    function buildMessage(message, convo) {
        const isUser = message.role === "user";
        const agentKey = message.agent || convo.agent || state.agent;
        const meta = AGENTS[agentKey] || AGENTS.chat;

        const row = document.createElement("div");
        row.className = "msg " + (isUser ? "user" : "assistant");

        const avatar = document.createElement("div");
        avatar.className = "avatar " + (isUser ? "" : agentKey);
        avatar.textContent = isUser ? "You" : meta.letter;

        const body = document.createElement("div");
        body.className = "msg-body";

        const metaRow = document.createElement("div");
        metaRow.className = "msg-meta";
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = isUser ? "You" : meta.label;
        metaRow.appendChild(name);
        if (message.ts) {
            const time = document.createElement("span");
            time.textContent = new Date(message.ts).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
            });
            metaRow.appendChild(time);
        }

        const bubble = document.createElement("div");
        bubble.className = "bubble";
        if (message.error) bubble.classList.add("error");

        if (isUser) {
            bubble.textContent = message.content;
        } else {
            bubble.innerHTML = renderMarkdown(message.content || "");
            const actions = document.createElement("div");
            actions.className = "msg-actions";
            actions.appendChild(buildCopyButton(message.content || ""));
            body.appendChild(metaRow);
            body.appendChild(bubble);
            body.appendChild(actions);
            row.appendChild(avatar);
            row.appendChild(body);
            return row;
        }

        body.appendChild(metaRow);
        body.appendChild(bubble);
        row.appendChild(avatar);
        row.appendChild(body);
        return row;
    }

    function buildCopyButton(text) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "copy-btn";
        btn.textContent = "Copy";
        btn.addEventListener("click", async () => {
            try {
                await navigator.clipboard.writeText(text);
                btn.textContent = "Copied";
            } catch (e) {
                btn.textContent = "Copy failed";
            }
            setTimeout(() => {
                btn.textContent = "Copy";
            }, 1200);
        });
        return btn;
    }

    function buildTyping(agentKey) {
        const meta = AGENTS[agentKey] || AGENTS.chat;
        const row = document.createElement("div");
        row.className = "msg assistant";
        row.dataset.typing = "true";

        const avatar = document.createElement("div");
        avatar.className = "avatar " + (meta.key || "chat");
        avatar.textContent = meta.letter;

        const body = document.createElement("div");
        body.className = "msg-body";
        const metaRow = document.createElement("div");
        metaRow.className = "msg-meta";
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = meta.label;
        metaRow.appendChild(name);

        const typing = document.createElement("div");
        typing.className = "typing";
        typing.innerHTML = "<span></span><span></span><span></span>";

        body.appendChild(metaRow);
        body.appendChild(typing);
        row.appendChild(avatar);
        row.appendChild(body);
        return row;
    }

    function scrollToBottom() {
        requestAnimationFrame(() => {
            el.chat.scrollTop = el.chat.scrollHeight;
        });
    }

    /* ---------------- markdown ---------------- */

    function escapeHtml(text) {
        return String(text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function inlineFormat(text) {
        return text
            .replace(/`([^`]+)`/g, "<code>$1</code>")
            .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
            .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    }

    function renderMarkdown(markdown) {
        const parts = escapeHtml(markdown).split("```");
        return parts
            .map((part, index) => {
                if (index % 2 === 1) {
                    const code = part.replace(/^[a-zA-Z0-9_+-]*\n/, "");
                    return "<pre><code>" + code.replace(/\n$/, "") + "</code></pre>";
                }
                return renderTextBlocks(part);
            })
            .join("");
    }

    function renderTextBlocks(text) {
        const lines = text.split("\n");
        const out = [];
        let list = null;
        let listType = null;
        let paragraph = [];

        function flushList() {
            if (list) {
                out.push("<" + listType + ">" + list.join("") + "</" + listType + ">");
                list = null;
                listType = null;
            }
        }

        function flushParagraph() {
            if (paragraph.length) {
                out.push("<p>" + inlineFormat(paragraph.join(" ")) + "</p>");
                paragraph = [];
            }
        }

        lines.forEach((raw) => {
            const line = raw.replace(/\s+$/, "");
            const heading = line.match(/^\s*(#{1,3})\s+(.*)$/);
            const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
            const bullet = line.match(/^\s*[-*]\s+(.*)$/);

            if (ordered || bullet) {
                flushParagraph();
                const type = ordered ? "ol" : "ul";
                if (listType !== type) {
                    flushList();
                    listType = type;
                    list = [];
                }
                list.push("<li>" + inlineFormat(ordered ? ordered[1] : bullet[1]) + "</li>");
                return;
            }

            flushList();

            if (heading) {
                flushParagraph();
                const level = heading[1].length;
                out.push("<h" + level + ">" + inlineFormat(heading[2]) + "</h" + level + ">");
                return;
            }

            if (!line.trim()) {
                flushParagraph();
                return;
            }

            paragraph.push(line.trim());
        });

        flushList();
        flushParagraph();
        return out.join("");
    }

    /* ---------------- sending ---------------- */

    function ensureConversation(text) {
        let convo = activeConversation();
        if (convo) return convo;

        convo = {
            id: uid(),
            title: text.slice(0, 60),
            agent: state.agent,
            sessionId: null,
            messages: [],
            updatedAt: Date.now(),
        };
        state.conversations.unshift(convo);
        state.activeId = convo.id;
        return convo;
    }

    async function sendMessage(text) {
        const message = String(text || "").trim();
        if (!message || state.sending) return;

        const convo = ensureConversation(message);
        convo.agent = state.agent;
        convo.messages.push({
            role: "user",
            content: message,
            agent: state.agent,
            ts: Date.now(),
        });
        convo.updatedAt = Date.now();

        state.sending = true;
        setComposerBusy(true);
        saveConversations();
        renderHistory();
        renderConversation();

        el.input.value = "";
        resizeInput();
        updateSendState();

        let assistantMessage;
        try {
            const response = await fetchJSON("/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    message: message,
                    session_id: convo.sessionId || null,
                    agent: state.agent,
                }),
            });

            convo.sessionId = response.session_id || convo.sessionId;
            assistantMessage = {
                role: "assistant",
                content: response.response || "(empty response)",
                agent: state.agent,
                ts: Date.now(),
            };
        } catch (error) {
            assistantMessage = {
                role: "assistant",
                content: error.message || "Request failed.",
                agent: state.agent,
                error: true,
                ts: Date.now(),
            };
        }

        convo.messages.push(assistantMessage);
        convo.updatedAt = Date.now();
        state.sending = false;
        setComposerBusy(false);
        saveConversations();
        renderHistory();
        renderConversation();
        updateRuntimeStatus(assistantMessage.error ? "error" : "online");
    }

    async function fetchJSON(url, options) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 120000);
        let response;
        try {
            response = await fetch(url, Object.assign({}, options, { signal: controller.signal }));
        } catch (error) {
            clearTimeout(timer);
            throw new Error(
                error.name === "AbortError"
                    ? "The request timed out. Please try again."
                    : "Could not reach the runtime. Check that the server is running."
            );
        }
        clearTimeout(timer);

        const raw = await response.text();
        let data = {};
        try {
            data = raw ? JSON.parse(raw) : {};
        } catch (e) {
            data = { detail: raw };
        }

        if (!response.ok) {
            const detail = data.detail || data.error || "Request failed with status " + response.status;
            throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        return data;
    }

    function setComposerBusy(busy) {
        el.input.disabled = busy;
        el.sendBtn.disabled = busy || !el.input.value.trim();
        if (!busy) el.input.focus();
    }

    function updateSendState() {
        el.sendBtn.disabled = state.sending || !el.input.value.trim();
    }

    /* ---------------- composer ---------------- */

    function resizeInput() {
        el.input.style.height = "auto";
        el.input.style.height = Math.min(el.input.scrollHeight, 190) + "px";
    }

    el.input.addEventListener("input", () => {
        resizeInput();
        updateSendState();
    });

    el.input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            el.form.requestSubmit();
        }
    });

    el.form.addEventListener("submit", (event) => {
        event.preventDefault();
        sendMessage(el.input.value);
    });

    el.newChatBtn.addEventListener("click", () => {
        if (state.sending) return;
        state.activeId = null;
        safeRemove(ACTIVE_KEY);
        renderWelcome();
        renderHistory();
        el.input.value = "";
        resizeInput();
        updateSendState();
        closeSidebar();
        el.input.focus();
    });

    /* ---------------- runtime status ---------------- */

    function updateRuntimeStatus(status) {
        el.statusDot.className = "dot " + (status === "online" ? "online" : status === "error" ? "offline" : "");
        el.statusText.textContent =
            status === "online" ? "Runtime online" : status === "error" ? "Last request failed" : "Checking runtime…";
    }

    fetch("/ping", { cache: "no-store" })
        .then((res) => updateRuntimeStatus(res.ok ? "online" : "offline"))
        .catch(() => updateRuntimeStatus("offline"));

    /* ---------------- sidebar ---------------- */

    const SIDEBAR_KEY = "agentcore.sidebar";
    const desktopQuery = window.matchMedia("(min-width: 881px)");

    function sidebarCollapsed() {
        return document.documentElement.getAttribute("data-sidebar") === "collapsed";
    }

    function syncSidebarA11y() {
        const collapsed = sidebarCollapsed() && desktopQuery.matches;
        el.collapseBtn.setAttribute("aria-expanded", String(!collapsed));
        el.expandBtn.setAttribute("aria-expanded", String(!collapsed));
    }

    function setSidebarCollapsed(collapsed) {
        document.documentElement.setAttribute("data-sidebar", collapsed ? "collapsed" : "expanded");
        safeSet(SIDEBAR_KEY, collapsed ? "collapsed" : "expanded");
        if (!desktopQuery.matches) closeSidebar();
        syncSidebarA11y();
    }

    el.collapseBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        setSidebarCollapsed(true);
    });
    el.expandBtn.addEventListener("click", () => setSidebarCollapsed(false));
    el.sidebar.addEventListener("click", (event) => {
        if (event.target.closest(".collapse-btn")) return;
        if (sidebarCollapsed() && desktopQuery.matches) setSidebarCollapsed(false);
    });
    desktopQuery.addEventListener("change", syncSidebarA11y);
    syncSidebarA11y();

    /* ---------------- mobile ---------------- */

    function openSidebar() {
        el.sidebar.classList.add("open");
        el.backdrop.hidden = false;
    }

    function closeSidebar() {
        el.sidebar.classList.remove("open");
        el.backdrop.hidden = true;
    }

    el.menuBtn.addEventListener("click", openSidebar);
    el.closeSidebar.addEventListener("click", closeSidebar);
    el.backdrop.addEventListener("click", closeSidebar);
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeSidebar();
    });

    /* ---------------- init ---------------- */

    applyTheme(document.documentElement.getAttribute("data-theme") || "dark");
    setAgent(state.agent, false);
    renderHistory();

    if (state.activeId && activeConversation()) {
        renderConversation();
    } else {
        state.activeId = null;
        renderWelcome();
    }

    updateSendState();
    el.input.focus();
})();

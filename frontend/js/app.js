/**
 * DTN-Manager — Unified Application
 *
 * Features:
 * - Immediate state sync (every mutation → fetchTopology)
 * - Per-node terminal tabs with WebSocket log streaming
 * - Resizable panels (left, right sidebars + bottom terminal bar)
 * - No auto-layout when adding nodes (only on explicit "Layout" button)
 */

(function () {
    'use strict';

    // ═══════════════════════════════════════════════════════════
    //  STATE
    // ═══════════════════════════════════════════════════════════

    let topology = { nodes: [], links: [] };
    let cy = null;
    let linkMode = false;
    let linkFirst = null;
    let pollTimer = null;
    let importInProgress = false;  // gates topology sync during import
    let wsTopology = null;

    // Per-node terminal state
    const nodeTerminals = {};   // nodeId → { ws, panelEl, tabEl }
    let activeTerminalId = null;

    // Pending positions from scenario import (used by renderGraph)
    let pendingPositions = {};
    let bundleStats = {};       // nodeId (string) → { src, fwd, rcv, dlv, exp, ... }
    let statsTimer = null;
    let statsVisible = true;

    // Server-truth reconciliation: the backend stamps every topology with a
    // monotonic epoch and a per-process boot_id. When either changes we flush
    // stale bundleStats so badges can't survive a node-id reuse or a restart.
    let serverEpoch = null;
    let serverBootId = null;

    // Connection health, driven by both the poll loop and the topology socket.
    let connectionState = 'connecting';   // connecting | connected | disconnected
    let consecutiveFailures = 0;

    // Single in-flight mutation lock (anti double-submit across all actions).
    let mutationInFlight = false;

    // ═══════════════════════════════════════════════════════════
    //  LOADING OVERLAY
    // ═══════════════════════════════════════════════════════════

    function showLoading(message, detail = '') {
        const overlay = document.getElementById('loading-overlay');
        document.getElementById('loading-message').textContent = message;
        document.getElementById('loading-detail').textContent = detail;
        overlay.classList.remove('hidden');
    }

    function updateLoading(message, detail) {
        document.getElementById('loading-message').textContent = message;
        if (detail !== undefined) {
            document.getElementById('loading-detail').textContent = detail;
        }
    }

    function hideLoading() {
        document.getElementById('loading-overlay').classList.add('hidden');
    }

    // ═══════════════════════════════════════════════════════════
    //  API HELPERS
    // ═══════════════════════════════════════════════════════════

    // One fetch chokepoint: read the body as text first, parse JSON defensively,
    // and check res.ok BEFORE trusting the parse — so a non-JSON error body
    // (a 502/504 HTML page, a crash) surfaces the real status instead of the
    // misleading "Unexpected token <" SyntaxError that used to hide the 409.
    async function apiFetch(url, opts = {}) {
        let res;
        try {
            res = await fetch(url, opts);
        } catch (e) {
            throw new Error(`Network error: ${e.message || 'request failed'}`);
        }
        const raw = await res.text();
        let data = null;
        if (raw) {
            try { data = JSON.parse(raw); } catch (e) { data = null; }
        }
        if (!res.ok) {
            const detail = (data && data.detail) ||
                (raw && raw.slice(0, 200)) ||
                `${res.status} ${res.statusText}`;
            throw new Error(detail);
        }
        return data;
    }

    async function apiPost(url, body = {}) {
        return apiFetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    }

    async function apiDelete(url) {
        return apiFetch(url, { method: 'DELETE' });
    }

    // Single in-flight mutation gate. Every mutating action funnels through
    // this so a double-click cannot fire two creates and spawn duplicate
    // resources. On any failure we force an immediate resync from server truth.
    async function runMutation(fn) {
        if (mutationInFlight) {
            toast('Another operation is in progress…', 'info');
            return;
        }
        mutationInFlight = true;
        try {
            return await fn();
        } catch (e) {
            toast(e.message || 'Operation failed', 'error');
            // Reconcile UI to whatever the server actually has.
            try { await fetchTopology(); } catch (_) {}
        } finally {
            mutationInFlight = false;
            hideLoading();
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  CONNECTION STATE
    // ═══════════════════════════════════════════════════════════

    function setConnectionState(state) {
        if (state === connectionState) return;
        connectionState = state;
        const dot = document.getElementById('conn-indicator');
        if (dot) {
            const colors = { connected: '#3fb950', connecting: '#d29922',
                             disconnected: '#f85149' };
            dot.style.background = colors[state] || '#888';
            dot.title = `Server: ${state}`;
        }
        if (state === 'disconnected') {
            toast('Lost connection to server — retrying…', 'error');
        }
        // Gate mutating buttons when we are not connected.
        const disabled = state === 'disconnected';
        document.querySelectorAll('[data-mutating="true"]').forEach(btn => {
            btn.disabled = disabled;
        });
    }

    function ensureConnIndicator() {
        if (document.getElementById('conn-indicator')) return;
        const dot = document.createElement('span');
        dot.id = 'conn-indicator';
        dot.className = 'conn-indicator conn-connecting';
        dot.title = 'Server: connecting';
        dot.style.cssText =
            'display:inline-block;width:10px;height:10px;border-radius:50%;' +
            'margin:0 8px;vertical-align:middle;background:#888;';
        const header = document.querySelector('header') || document.body;
        header.insertBefore(dot, header.firstChild);
    }

    function markPollOk() {
        consecutiveFailures = 0;
        setConnectionState('connected');
    }

    function markPollFail() {
        consecutiveFailures += 1;
        if (consecutiveFailures >= 3) {
            setConnectionState('disconnected');
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  TOPOLOGY SYNC
    // ═══════════════════════════════════════════════════════════

    // Flush per-node stats when the server reports a new epoch or a restart,
    // so a stale badge can never outlive the node it described.
    function reconcileEpoch(data) {
        if (!data) return;
        const epoch = data.epoch;
        const bootId = data.boot_id;
        if ((serverBootId !== null && bootId !== serverBootId) ||
            (serverEpoch !== null && epoch !== serverEpoch)) {
            bundleStats = {};
        }
        serverEpoch = epoch;
        serverBootId = bootId;
    }

    async function fetchTopology() {
        if (importInProgress) return;  // skip while import is running
        try {
            const data = await apiFetch('/api/topology');
            reconcileEpoch(data);
            topology = data;
            renderGraph();
            updateUI();
            syncTerminals();
            markPollOk();
        } catch (e) {
            markPollFail();
        }
    }

    async function syncAfterMutation() {
        await new Promise(r => setTimeout(r, 300));
        await fetchTopology();
    }

    function startPolling() {
        if (pollTimer) return;
        pollTimer = setInterval(fetchTopology, 3000);
    }

    async function fetchBundleStats() {
        if (topology.nodes.length === 0) return;
        try {
            const data = await apiFetch('/api/bundle-stats');
            // Keep only stats for nodes that currently exist (drop stale ids).
            const liveIds = new Set(topology.nodes.map(n => String(n.id)));
            const filtered = {};
            for (const [id, v] of Object.entries(data || {})) {
                if (liveIds.has(String(id))) filtered[id] = v;
            }
            bundleStats = filtered;
            renderNodeStats();
        } catch (e) { /* retry next poll */ }
    }

    function startStatsPolling() {
        if (statsTimer) return;
        statsTimer = setInterval(fetchBundleStats, 5000);
    }

    function renderNodeStats() {
        const layer = document.getElementById('node-stats-layer');
        if (!layer) return;
        layer.innerHTML = '';
        if (!cy || !statsVisible) return;

        const container = document.getElementById('cy');
        if (!container) return;

        for (const node of topology.nodes) {
            const stats = bundleStats[String(node.id)];
            if (!stats) continue;

            // Skip nodes with all zeros
            const total = (stats.src || 0) + (stats.dlv || 0) + (stats.exp || 0) + (stats.rcv || 0);
            if (total === 0) continue;

            // Get rendered (screen) position of the node
            const cyNode = cy.getElementById(`n${node.id}`);
            if (!cyNode || cyNode.length === 0) continue;
            const rPos = cyNode.renderedPosition();

            // Estimate buffered = rcv - dlv - fwd (bundles in this node's buffer)
            const buffered = Math.max(0, (stats.rcv || 0) - (stats.dlv || 0) - (stats.fwd || 0));

            // Position badge above the node — account for zoom so it doesn't
            // overlap the label.  Node is 54px model-space, but on screen it's
            // 54 * zoom.  We want the badge bottom-edge above the node top.
            const nodeScreenR = 27 * cy.zoom();  // rendered radius
            const badge = document.createElement('div');
            badge.className = 'node-stats-badge';
            badge.style.left = rPos.x + 'px';
            badge.style.top = (rPos.y - nodeScreenR - 10) + 'px';  // 10px gap above node

            let rows = '';
            if (stats.src > 0) {
                rows += `<div class="stats-row"><span class="stat-label">src</span><span class="stat-val src">${stats.src}</span></div>`;
            }
            if (stats.fwd > 0) {
                rows += `<div class="stats-row"><span class="stat-label">fwd</span><span class="stat-val fwd">${stats.fwd}</span></div>`;
            }
            if (stats.rcv > 0) {
                rows += `<div class="stats-row"><span class="stat-label">rcv</span><span class="stat-val rcv">${stats.rcv}</span></div>`;
            }
            if (stats.dlv > 0 || stats.bpsink_delivered > 0) {
                const dlvCount = stats.bpsink_delivered || stats.dlv || 0;
                rows += `<div class="stats-row"><span class="stat-label">dlv</span><span class="stat-val dlv">${dlvCount}</span></div>`;
            }
            if (stats.exp > 0) {
                rows += `<div class="stats-row"><span class="stat-label">exp</span><span class="stat-val exp">${stats.exp}</span></div>`;
            }
            if (buffered > 0) {
                rows += `<div class="stats-row"><span class="stat-label">buf</span><span class="stat-val buf">${buffered}</span></div>`;
            }

            if (rows) {
                badge.innerHTML = rows;
                layer.appendChild(badge);
            }
        }
    }

    // Re-render stats when graph pans/zooms
    function attachStatsSync() {
        if (!cy) return;
        cy.on('pan zoom', () => renderNodeStats());
        cy.on('position', 'node', () => renderNodeStats());
    }

    // ═══════════════════════════════════════════════════════════
    //  WEBSOCKET — topology (optional, polling is primary)
    // ═══════════════════════════════════════════════════════════

    // One resilient-socket factory: capped exponential backoff + jitter, a
    // single pending reconnect timer (no leaked timers/handlers), and cleanup
    // on each (re)connect. The socket OWNS its own reconnection — it is the
    // single recovery owner, so nothing else recreates it underneath.
    function makeResilientSocket(url, { onMessage, onOpen, onClose } = {}) {
        let ws = null;
        let reconnectTimer = null;
        let attempt = 0;
        let closedByUser = false;

        function connect() {
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
            try {
                ws = new WebSocket(url);
            } catch (e) {
                scheduleReconnect();
                return;
            }
            ws.onopen = () => {
                attempt = 0;
                if (onOpen) try { onOpen(); } catch (_) {}
            };
            ws.onmessage = (evt) => {
                if (onMessage) try { onMessage(evt); } catch (_) {}
            };
            ws.onerror = () => { try { ws.close(); } catch (_) {} };
            ws.onclose = () => {
                if (onClose) try { onClose(); } catch (_) {}
                if (!closedByUser) scheduleReconnect();
            };
        }

        function scheduleReconnect() {
            if (closedByUser || reconnectTimer) return;
            attempt += 1;
            // Cap at 30s; add jitter to avoid synchronized reconnect storms.
            const base = Math.min(30000, 1000 * Math.pow(2, attempt));
            const delay = base / 2 + Math.random() * (base / 2);
            reconnectTimer = setTimeout(() => {
                reconnectTimer = null;
                connect();
            }, delay);
        }

        connect();
        return {
            close() {
                closedByUser = true;
                if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
                if (ws) try { ws.close(); } catch (_) {}
            },
            get readyState() { return ws ? ws.readyState : WebSocket.CLOSED; },
        };
    }

    function connectWsTopology() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        wsTopology = makeResilientSocket(
            `${proto}//${location.host}/ws/topology`,
            {
                onMessage: (evt) => {
                    if (importInProgress) return;  // skip while import is running
                    let msg;
                    try { msg = JSON.parse(evt.data); } catch (e) { return; }
                    if (msg.type === 'topology') {
                        reconcileEpoch(msg.data);
                        topology = msg.data;
                        renderGraph();
                        updateUI();
                        syncTerminals();
                        markPollOk();
                    }
                },
            }
        );
    }

    // ═══════════════════════════════════════════════════════════
    //  PER-NODE TERMINALS
    // ═══════════════════════════════════════════════════════════

    function syncTerminals() {
        const currentIds = new Set(topology.nodes.map(n => n.id));

        // Add terminals for new nodes. Reconnection is owned by the log socket
        // itself (makeResilientSocket), so we never recreate it here — doing so
        // would race the socket's own backoff and leak handlers/timers.
        for (const node of topology.nodes) {
            if (!nodeTerminals[node.id]) {
                createTerminal(node.id, node.name);
            }
        }

        // Remove terminals for deleted nodes
        for (const id of Object.keys(nodeTerminals)) {
            if (!currentIds.has(parseInt(id))) {
                destroyTerminal(parseInt(id));
            }
        }

        // Auto-activate first terminal if none active
        if (activeTerminalId === null && topology.nodes.length > 0) {
            activateTerminal(topology.nodes[0].id);
        }
    }

    function createTerminal(nodeId, name) {
        const tabsEl = document.getElementById('terminal-tabs');
        const panelsEl = document.getElementById('terminal-panels');

        // Remove empty message if present
        const emptyMsg = panelsEl.querySelector('.terminal-empty');
        if (emptyMsg) emptyMsg.remove();

        // Create tab
        const tab = document.createElement('button');
        tab.className = 'terminal-tab';
        tab.innerHTML = `<span class="tab-dot"></span>${name}`;
        tab.onclick = () => activateTerminal(nodeId);
        tabsEl.appendChild(tab);

        // Create panel
        const panel = document.createElement('div');
        panel.className = 'terminal-panel';
        panel.innerHTML = `<div class="log-info">── Terminal for ${name} (ipn:${nodeId}) ──</div>`;
        panelsEl.appendChild(panel);

        // Connect a resilient WebSocket for log streaming (self-reconnecting).
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = makeResilientSocket(
            `${proto}//${location.host}/ws/logs/${nodeId}`,
            {
                onMessage: (evt) => {
                    let msg;
                    try { msg = JSON.parse(evt.data); } catch (e) { return; }
                    if (msg.type === 'logs') {
                        appendToTerminal(nodeId, msg.lines);
                    }
                },
            }
        );

        nodeTerminals[nodeId] = { ws, panelEl: panel, tabEl: tab };

        // Activate this terminal
        activateTerminal(nodeId);
    }

    function destroyTerminal(nodeId) {
        const term = nodeTerminals[nodeId];
        if (!term) return;
        if (term.ws) term.ws.close();
        term.tabEl.remove();
        term.panelEl.remove();
        delete nodeTerminals[nodeId];

        if (activeTerminalId === nodeId) {
            activeTerminalId = null;
            // Activate first remaining
            const remaining = Object.keys(nodeTerminals);
            if (remaining.length > 0) {
                activateTerminal(parseInt(remaining[0]));
            } else {
                showTerminalEmpty();
            }
        }
    }

    function activateTerminal(nodeId) {
        activeTerminalId = nodeId;

        // Update tabs
        for (const [id, term] of Object.entries(nodeTerminals)) {
            term.tabEl.classList.toggle('active', parseInt(id) === nodeId);
            term.panelEl.classList.toggle('active', parseInt(id) === nodeId);
        }
    }

    function appendToTerminal(nodeId, lines) {
        const term = nodeTerminals[nodeId];
        if (!term) return;
        const panel = term.panelEl;

        for (const line of lines) {
            const div = document.createElement('div');
            if (line.includes("'") || line.includes('Payload')) {
                div.className = 'log-bundle';
            }
            div.textContent = line;
            panel.appendChild(div);
        }

        // Keep max 500 lines
        while (panel.children.length > 500) {
            panel.removeChild(panel.firstChild);
        }

        panel.scrollTop = panel.scrollHeight;
    }

    function showTerminalEmpty() {
        const panelsEl = document.getElementById('terminal-panels');
        if (!panelsEl.querySelector('.terminal-empty')) {
            const el = document.createElement('div');
            el.className = 'terminal-empty';
            el.textContent = 'No nodes — add a node to see its terminal';
            panelsEl.appendChild(el);
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  CYTOSCAPE GRAPH
    // ═══════════════════════════════════════════════════════════

    function initGraph() {
        cy = cytoscape({
            container: document.getElementById('cy'),
            style: [
                {
                    selector: 'node',
                    style: {
                        'label': 'data(label)',
                        'background-color': '#58a6ff',
                        'border-width': 2, 'border-color': '#79bfff',
                        'color': '#e6edf3',
                        'text-valign': 'center', 'text-halign': 'center',
                        'font-size': '15px', 'font-weight': '600',
                        'width': 54, 'height': 54,
                        'text-outline-width': 2, 'text-outline-color': '#0d1117',
                    }
                },
                {
                    selector: 'node:selected',
                    style: {
                        'background-color': '#bc8cff', 'border-color': '#d2a8ff',
                        'border-width': 3, 'width': 60, 'height': 60,
                    }
                },
                {
                    selector: 'node.link-candidate',
                    style: { 'border-color': '#3fb950', 'border-width': 3 }
                },
                {
                    selector: 'node.link-first',
                    style: {
                        'background-color': '#3fb950', 'border-color': '#56d364',
                        'border-width': 3,
                    }
                },
                {
                    selector: 'edge',
                    style: {
                        'width': 3, 'line-color': '#3fb950',
                        'curve-style': 'bezier', 'target-arrow-shape': 'none',
                        'opacity': 0.8,
                    }
                },
                {
                    selector: 'edge[status="down"]',
                    style: {
                        'line-color': '#f85149', 'line-style': 'dashed',
                        'opacity': 0.5, 'width': 2,
                    }
                },
                {
                    selector: 'edge[status="degraded"]',
                    style: { 'line-color': '#d29922', 'line-style': 'dashed', 'opacity': 0.7 }
                },
                {
                    selector: 'edge:selected',
                    style: { 'width': 5, 'opacity': 1 }
                },
            ],
            layout: { name: 'preset' },
            minZoom: 0.3, maxZoom: 3, wheelSensitivity: 0.3,
            autoungrabify: false,
        });


        cy.on('tap', 'node', (evt) => {
            const nodeId = parseInt(evt.target.id().replace('n', ''));
            if (linkMode) { handleLinkClick(nodeId); return; }
            showNodeInspector(nodeId);
        });

        cy.on('tap', 'edge', (evt) => {
            if (linkMode) return;
            showLinkInspector(evt.target.id().replace('link_', ''));
        });

        cy.on('tap', (evt) => {
            if (evt.target === cy) { clearInspector(); dismissContextMenu(); }
        });

        // Right-click context menu on nodes — use native DOM event
        document.getElementById('cy').addEventListener('contextmenu', (e) => {
            e.preventDefault();

            // Convert screen position to Cytoscape model coordinates
            const container = document.getElementById('cy');
            const rect = container.getBoundingClientRect();
            const pos = {
                x: e.clientX - rect.left,
                y: e.clientY - rect.top,
            };

            // Find node at this position
            const pan = cy.pan();
            const zoom = cy.zoom();
            const modelX = (pos.x - pan.x) / zoom;
            const modelY = (pos.y - pan.y) / zoom;

            let hitNode = null;
            cy.nodes().forEach((n) => {
                const np = n.position();
                const w = n.width() / 2;
                const h = n.height() / 2;
                if (Math.abs(modelX - np.x) <= w && Math.abs(modelY - np.y) <= h) {
                    hitNode = n;
                }
            });

            if (hitNode) {
                const nodeId = parseInt(hitNode.id().replace('n', ''));
                const node = topology.nodes.find(n => n.id === nodeId);
                if (node) {
                    showNodeContextMenu(nodeId, node.name, e.clientX, e.clientY);
                }
                return;
            }

            // Check for edge hit (within ~8px tolerance of any edge midpoint)
            let hitEdge = null;
            let bestDist = 12;  // px threshold in model coords
            cy.edges().forEach((edge) => {
                const src = edge.source().position();
                const tgt = edge.target().position();
                // Midpoint and distance from click to edge line segment
                const dx = tgt.x - src.x;
                const dy = tgt.y - src.y;
                const lenSq = dx * dx + dy * dy;
                if (lenSq === 0) return;
                let t = ((modelX - src.x) * dx + (modelY - src.y) * dy) / lenSq;
                t = Math.max(0, Math.min(1, t));
                const closestX = src.x + t * dx;
                const closestY = src.y + t * dy;
                const dist = Math.sqrt((modelX - closestX) ** 2 + (modelY - closestY) ** 2);
                if (dist < bestDist) {
                    bestDist = dist;
                    hitEdge = edge;
                }
            });

            if (hitEdge) {
                const linkId = hitEdge.id().replace('link_', '');
                const link = topology.links.find(l => l.id === linkId);
                if (link) {
                    const nameA = topology.nodes.find(n => n.id === link.node_a)?.name || `N${link.node_a}`;
                    const nameB = topology.nodes.find(n => n.id === link.node_b)?.name || `N${link.node_b}`;
                    showLinkContextMenu(linkId, nameA, nameB, link.status, e.clientX, e.clientY);
                }
                return;
            }

            dismissContextMenu();
        });
    }

    /**
     * Render graph — NEVER auto-layout.
     * New nodes are placed near viewport center with a small random offset.
     */
    function renderGraph() {
        if (!cy) return;

        const existingNodes = new Set();
        const existingEdges = new Set();

        // Use viewport center for new node placement
        const extent = cy.extent();
        const cx = (extent.x1 + extent.x2) / 2;
        const cy_center = (extent.y1 + extent.y2) / 2;

        // Sync nodes
        for (const node of topology.nodes) {
            const id = `n${node.id}`;
            existingNodes.add(id);

            const el = cy.getElementById(id);
            if (el.length === 0) {
                // Check for saved position from import, otherwise place near center
                let px, py;
                if (pendingPositions[id]) {
                    px = pendingPositions[id].x;
                    py = pendingPositions[id].y;
                } else {
                    const angle = (Object.keys(topology.nodes).length * 2.4) + node.id;
                    const r = 80 + Math.random() * 60;
                    px = cx + Math.cos(angle) * r;
                    py = cy_center + Math.sin(angle) * r;
                }
                cy.add({
                    group: 'nodes',
                    data: { id, label: node.name, nodeId: node.id },
                    position: { x: px, y: py },
                });
            }
        }

        // Sync edges
        for (const link of topology.links) {
            const id = `link_${link.id}`;
            existingEdges.add(id);
            const el = cy.getElementById(id);
            if (el.length === 0) {
                cy.add({
                    group: 'edges',
                    data: {
                        id, source: `n${link.node_a}`, target: `n${link.node_b}`,
                        status: link.status,
                    },
                });
            } else {
                el.data('status', link.status);
            }
        }

        // Remove stale elements
        cy.nodes().forEach(n => { if (!existingNodes.has(n.id())) n.remove(); });
        cy.edges().forEach(e => { if (!existingEdges.has(e.id())) e.remove(); });
    }

    function runLayout() {
        if (cy && cy.nodes().length > 0) {
            cy.layout({
                name: 'cose', animate: true, animationDuration: 400,
                nodeRepulsion: 6000, idealEdgeLength: 130, gravity: 0.4, padding: 50,
            }).run();
        }
    }

    /**
     * Circle layout — N1 at 9-o'clock, then N2, N3… clockwise.
     * Uses direct position() calls for maximum reliability.
     */
    function runCircleLayout() {
        console.log('[CircleLayout] called, cy=', !!cy, 'nodes=', cy ? cy.nodes().length : 0);
        if (!cy || cy.nodes().length === 0) return;

        const sorted = cy.nodes().toArray().sort((a, b) =>
            a.data('nodeId') - b.data('nodeId')
        );

        const n = sorted.length;
        const radius = Math.max(150, n * 50);

        // In screen coords (Y-down), clockwise from 9 o'clock (π):
        //   π → 3π/2 (top) → 2π (right) → 5π/2 (bottom)
        sorted.forEach((node, i) => {
            const angle = Math.PI + (2 * Math.PI * i) / n;
            const x = radius * Math.cos(angle);
            const y = radius * Math.sin(angle);
            console.log(`[CircleLayout] ${node.data('label')} (id=${node.data('nodeId')}) -> angle=${(angle*180/Math.PI).toFixed(0)}deg pos=(${x.toFixed(0)}, ${y.toFixed(0)})`);
            node.position({ x, y });
        });

        cy.fit(undefined, 50);
        console.log('[CircleLayout] done');
    }

    // ===============================================================
    //  CONTEXT MENU (right-click on nodes)
    // ===============================================================

    function dismissContextMenu() {
        const container = document.getElementById('ctx-menu-container');
        if (container) container.innerHTML = '';
    }

    function dismissModal() {
        const existing = document.querySelector('.modal-overlay');
        if (existing) existing.remove();
    }

    function showNodeContextMenu(nodeId, nodeName, x, y) {
        dismissContextMenu();

        const menu = document.createElement('div');
        menu.className = 'ctx-menu';

        // Clamp position to viewport
        const menuW = 220, menuH = 300;
        const vw = window.innerWidth, vh = window.innerHeight;
        if (x + menuW > vw) x = vw - menuW - 8;
        if (y + menuH > vh) y = vh - menuH - 8;
        menu.style.left = x + 'px';
        menu.style.top = y + 'px';

        menu.innerHTML = `
            <div class="ctx-menu-header">
                <span class="ctx-header-dot"></span>
                ${nodeName}
            </div>
            <div class="ctx-menu-divider"></div>
            <div class="ctx-menu-item" data-action="set-exits">
                <span class="ctx-icon" style="color:#3fb950">R</span>
                <span class="ctx-label">Set All Exits</span>
            </div>
            <div class="ctx-menu-item" data-action="add-exit">
                <span class="ctx-icon" style="color:#58a6ff">+</span>
                <span class="ctx-label">Add Exit...</span>
            </div>
            <div class="ctx-menu-divider"></div>
            <div class="ctx-menu-item" data-action="view-config">
                <span class="ctx-icon" style="color:#bc8cff">C</span>
                <span class="ctx-label">View Config</span>
            </div>
            <div class="ctx-menu-item" data-action="view-logs">
                <span class="ctx-icon" style="color:#d29922">L</span>
                <span class="ctx-label">View Logs</span>
            </div>
            <div class="ctx-menu-item" data-action="terminal">
                <span class="ctx-icon" style="color:#8b949e">T</span>
                <span class="ctx-label">Terminal</span>
            </div>
            <div class="ctx-menu-divider"></div>
            <div class="ctx-menu-item danger" data-action="delete">
                <span class="ctx-icon">X</span>
                <span class="ctx-label">Delete Node</span>
            </div>
        `;

        // Handle clicks
        menu.addEventListener('click', async (e) => {
            const item = e.target.closest('.ctx-menu-item');
            if (!item) return;
            const action = item.dataset.action;
            dismissContextMenu();

            switch (action) {
                case 'set-exits':
                    showLoading('Setting Exits', `Computing shortest-path routes for ${nodeName}...`);
                    try {
                        await apiPost(`/api/nodes/${nodeId}/exits/auto`);
                        toast(`Exits set on ${nodeName} (shortest-path)`, 'success');
                    } catch (err) { toast(`Failed: ${err.message}`, 'error'); }
                    finally { hideLoading(); }
                    break;
                case 'add-exit':
                    showAddExitModal(nodeId, nodeName);
                    break;
                case 'view-config':
                    showNodeConfigModal(nodeId, nodeName);
                    break;
                case 'view-logs':
                    App.viewIonLog(nodeId);
                    break;
                case 'terminal':
                    App.focusTerminal(nodeId);
                    break;
                case 'delete':
                    App.deleteNode(nodeId);
                    break;
            }
        });

        const container = document.getElementById('ctx-menu-container');
        container.innerHTML = '';
        container.appendChild(menu);

        // Dismiss on any click outside
        const dismiss = (e) => {
            if (!menu.contains(e.target)) {
                dismissContextMenu();
                document.removeEventListener('mousedown', dismiss);
            }
        };
        setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
    }

    function showLinkContextMenu(linkId, nameA, nameB, status, x, y) {
        dismissContextMenu();

        const menu = document.createElement('div');
        menu.className = 'ctx-menu';

        const menuW = 220, menuH = 200;
        const vw = window.innerWidth, vh = window.innerHeight;
        if (x + menuW > vw) x = vw - menuW - 8;
        if (y + menuH > vh) y = vh - menuH - 8;
        menu.style.left = x + 'px';
        menu.style.top = y + 'px';

        const isDisrupted = (status === 'down' || status === 'degraded');
        const dotColor = isDisrupted ? 'var(--danger)' : 'var(--success)';

        menu.innerHTML = `
            <div class="ctx-menu-header">
                <span class="ctx-header-dot" style="background:${dotColor}"></span>
                ${nameA} ↔ ${nameB}
            </div>
            <div class="ctx-menu-divider"></div>
            <div class="ctx-menu-item" data-action="disrupt" style="${isDisrupted ? 'opacity:0.4;pointer-events:none' : ''}">
                <span class="ctx-icon" style="color:#f85149">✕</span>
                <span class="ctx-label">Disrupt Link</span>
            </div>
            <div class="ctx-menu-item" data-action="restore" style="${!isDisrupted ? 'opacity:0.4;pointer-events:none' : ''}">
                <span class="ctx-icon" style="color:#3fb950">✓</span>
                <span class="ctx-label">Restore Link</span>
            </div>
            <div class="ctx-menu-divider"></div>
            <div class="ctx-menu-item danger" data-action="delete">
                <span class="ctx-icon">X</span>
                <span class="ctx-label">Delete Link</span>
            </div>
        `;

        menu.addEventListener('click', async (e) => {
            const item = e.target.closest('.ctx-menu-item');
            if (!item) return;
            const action = item.dataset.action;
            dismissContextMenu();

            switch (action) {
                case 'disrupt':
                    await runMutation(async () => {
                        showLoading('Disrupting Link', `Disrupting ${nameA} ↔ ${nameB}...`);
                        await apiPost(`/api/links/${linkId}/disrupt`, { loss_percent: 100 });
                        toast(`Link ${nameA} ↔ ${nameB} disrupted`, 'info');
                        await syncAfterMutation();
                    });
                    break;
                case 'restore':
                    await runMutation(async () => {
                        showLoading('Restoring Link', `Restoring ${nameA} ↔ ${nameB}...`);
                        await apiPost(`/api/links/${linkId}/restore`);
                        toast(`Link ${nameA} ↔ ${nameB} restored`, 'success');
                        await syncAfterMutation();
                    });
                    break;
                case 'delete':
                    if (!confirm(`Delete link ${nameA} ↔ ${nameB}?`)) return;
                    await runMutation(async () => {
                        showLoading('Deleting Link', `Removing ${nameA} ↔ ${nameB}...`);
                        await apiDelete(`/api/links/${linkId}`);
                        toast(`Link ${nameA} ↔ ${nameB} deleted`, 'info');
                        await syncAfterMutation();
                        clearInspector();
                    });
                    break;
            }
        });

        const container = document.getElementById('ctx-menu-container');
        container.innerHTML = '';
        container.appendChild(menu);

        const dismiss = (e) => {
            if (!menu.contains(e.target)) {
                dismissContextMenu();
                document.removeEventListener('mousedown', dismiss);
            }
        };
        setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
    }

    async function showAddExitModal(nodeId, nodeName) {
        dismissModal();

        // Fetch neighbors for gateway dropdown
        let neighbors = [];
        try {
            const res = await fetch(`/api/nodes/${nodeId}/neighbors`);
            if (res.ok) {
                const data = await res.json();
                neighbors = data.neighbors || [];
            }
        } catch (e) {}

        if (neighbors.length === 0) {
            toast(`${nodeName} has no neighbors to use as gateway`, 'error');
            return;
        }

        // Build options
        const destOptions = topology.nodes
            .filter(n => n.id !== nodeId)
            .map(n => `<option value="${n.id}">N${n.id} (ipn:${n.id})</option>`)
            .join('');

        const gwOptions = neighbors
            .map(nid => `<option value="${nid}">N${nid} (ipn:${nid}.0)</option>`)
            .join('');

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal-card">
                <h3>Add Exit on ${nodeName}</h3>
                <div class="input-row">
                    <label class="input-label">Dest First</label>
                    <select id="modal-dest-first" class="input-full">${destOptions}</select>
                </div>
                <div class="input-row">
                    <label class="input-label">Dest Last</label>
                    <select id="modal-dest-last" class="input-full">${destOptions}</select>
                </div>
                <div class="input-row">
                    <label class="input-label">Gateway</label>
                    <select id="modal-gateway" class="input-full">${gwOptions}</select>
                </div>
                <div class="modal-actions">
                    <button class="btn btn-sm" id="modal-cancel">Cancel</button>
                    <button class="btn btn-accent btn-sm" id="modal-confirm">Add Exit</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        // Sync dest-last to match dest-first
        const df = document.getElementById('modal-dest-first');
        const dl = document.getElementById('modal-dest-last');
        dl.value = df.value;
        df.addEventListener('change', () => {
            if (parseInt(dl.value) < parseInt(df.value)) dl.value = df.value;
        });

        // Cancel
        document.getElementById('modal-cancel').onclick = () => dismissModal();
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) dismissModal();
        });

        // Confirm
        document.getElementById('modal-confirm').onclick = async () => {
            const destFirst = parseInt(df.value);
            const destLast = parseInt(dl.value);
            const gateway = parseInt(document.getElementById('modal-gateway').value);
            dismissModal();

            if (destLast < destFirst) {
                toast('Dest Last must be >= Dest First', 'error');
                return;
            }

            showLoading('Adding Exit', `${nodeName}: exit ${destFirst}-${destLast} via ipn:${gateway}.0`);
            try {
                await apiPost(`/api/nodes/${nodeId}/exits`, {
                    dest_first: destFirst,
                    dest_last: destLast,
                    gateway_id: gateway,
                });
                toast(`Exit added on ${nodeName}: ${destFirst}-${destLast} via ipn:${gateway}.0`, 'success');
            } catch (err) { toast(`Failed: ${err.message}`, 'error'); }
            finally { hideLoading(); }
        };
    }

    async function showNodeConfigModal(nodeId, nodeName) {
        dismissModal();
        showLoading('Loading Config', `Fetching ION config for ${nodeName}...`);

        let config;
        try {
            const res = await fetch(`/api/nodes/${nodeId}/config`);
            if (!res.ok) throw new Error('Failed to fetch config');
            config = await res.json();
        } catch (err) {
            hideLoading();
            toast(`Failed: ${err.message}`, 'error');
            return;
        }
        hideLoading();

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';

        let body = '';
        if (config.node_rc) {
            body += `<div class="config-section-title">node.rc</div>`;
            body += `<div class="config-modal-content">${escapeHtml(config.node_rc)}</div>`;
        }

        overlay.innerHTML = `
            <div class="modal-card" style="min-width:480px;max-width:600px">
                <h3>${nodeName} -- ION Configuration</h3>
                ${body}
                <div class="modal-actions">
                    <button class="btn btn-sm" id="config-close">Close</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);
        document.getElementById('config-close').onclick = () => dismissModal();
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) dismissModal();
        });
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ===============================================================
    //  UI UPDATES
    // ===============================================================

    function updateUI() {
        document.getElementById('node-count').textContent =
            `${topology.nodes.length} node${topology.nodes.length !== 1 ? 's' : ''}`;
        document.getElementById('link-count').textContent =
            `${topology.links.length} link${topology.links.length !== 1 ? 's' : ''}`;

        updateSelect('traffic-from', topology.nodes);
        updateSelect('traffic-to', topology.nodes);
        updateNodeList();
    }

    function updateSelect(id, nodes) {
        const el = document.getElementById(id);
        const prev = el.value;
        while (el.options.length > 1) el.remove(1);
        for (const n of nodes) {
            const opt = document.createElement('option');
            opt.value = n.id;
            opt.textContent = `N${n.id} (ipn:${n.id})`;
            el.appendChild(opt);
        }
        if (prev) el.value = prev;
    }

    function updateNodeList() {
        const el = document.getElementById('node-list');
        el.innerHTML = '';
        if (topology.nodes.length === 0) {
            el.innerHTML = '<p class="placeholder-text">No nodes yet</p>';
            return;
        }
        for (const n of topology.nodes) {
            const item = document.createElement('div');
            item.className = 'node-list-item';
            item.innerHTML = `
                <span class="node-list-dot"></span>
                <span class="node-list-name">${n.name}</span>
                <span class="node-list-stats">↓${n.stats.bundles_received} ↑${n.stats.bundles_sent || 0}</span>
            `;
            item.onclick = () => {
                cy.getElementById(`n${n.id}`).select();
                showNodeInspector(n.id);
            };
            el.appendChild(item);
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  INSPECTOR
    // ═══════════════════════════════════════════════════════════

    async function showNodeInspector(nodeId) {
        const node = topology.nodes.find(n => n.id === nodeId);
        if (!node) return;
        const ips = Object.entries(node.ip_addresses).map(
            ([k, v]) => `<div class="inspector-item"><span class="inspector-label">${k}</span><span class="inspector-value">${v}</span></div>`
        ).join('');

        document.getElementById('inspector-content').innerHTML = `
            <div class="inspector-item">
                <span class="inspector-label">Node</span>
                <span class="inspector-value">${node.name} (ipn:${node.id})</span>
            </div>
            <div class="inspector-item">
                <span class="inspector-label">Container</span>
                <span class="inspector-value">${node.container_name}</span>
            </div>
            <div class="inspector-item">
                <span class="inspector-label">Status</span>
                <span class="inspector-value">${node.status}</span>
            </div>
            <div class="inspector-item">
                <span class="inspector-label">Bundles In</span>
                <span class="inspector-value">${node.stats.bundles_received}</span>
            </div>
            ${ips}
            <div class="inspector-actions">
                <button class="btn btn-danger btn-sm" onclick="App.deleteNode(${node.id})">Delete</button>
                <button class="btn btn-sm" onclick="App.focusTerminal(${node.id})">Terminal</button>
                <button class="btn btn-sm" onclick="App.viewIonLog(${node.id})">ion.log</button>
            </div>
        `;
    }

    function showLinkInspector(linkId) {
        const link = topology.links.find(l => l.id === linkId);
        if (!link) return;
        const isDown = link.status === 'down';
        const isDeg = link.status === 'degraded';
        const sc = isDown ? '#f85149' : isDeg ? '#d29922' : '#3fb950';

        document.getElementById('inspector-content').innerHTML = `
            <div class="inspector-item">
                <span class="inspector-label">Link</span>
                <span class="inspector-value">N${link.node_a} ↔ N${link.node_b}</span>
            </div>
            <div class="inspector-item">
                <span class="inspector-label">Subnet</span>
                <span class="inspector-value">${link.subnet}</span>
            </div>
            <div class="inspector-item">
                <span class="inspector-label">IP A</span>
                <span class="inspector-value">${link.ip_a}</span>
            </div>
            <div class="inspector-item">
                <span class="inspector-label">IP B</span>
                <span class="inspector-value">${link.ip_b}</span>
            </div>
            <div class="inspector-item">
                <span class="inspector-label">Status</span>
                <span class="inspector-value" style="color:${sc}">${link.status.toUpperCase()}</span>
            </div>
            <div class="inspector-actions">
                ${isDown || isDeg
                    ? `<button class="btn btn-accent btn-sm" onclick="App.restoreLink('${link.id}')">✔ Restore</button>`
                    : `<button class="btn btn-warning btn-sm" onclick="App.disruptLink('${link.id}')">⚡ Disrupt</button>`
                }
                <button class="btn btn-danger btn-sm" onclick="App.deleteLink('${link.id}')">✕ Delete</button>
            </div>
        `;
    }

    function clearInspector() {
        document.getElementById('inspector-content').innerHTML =
            '<p class="placeholder-text">Click a node or link to inspect</p>';
    }

    // ═══════════════════════════════════════════════════════════
    //  LINK MODE
    // ═══════════════════════════════════════════════════════════

    function enterLinkMode() {
        linkMode = true; linkFirst = null;
        document.getElementById('btn-link-mode').classList.add('active');
        document.getElementById('mode-banner').classList.remove('hidden');
        document.getElementById('mode-text').textContent = 'Link mode: click first node';
        cy.nodes().addClass('link-candidate');
    }

    function exitLinkMode() {
        linkMode = false; linkFirst = null;
        document.getElementById('btn-link-mode').classList.remove('active');
        document.getElementById('mode-banner').classList.add('hidden');
        cy.nodes().removeClass('link-candidate').removeClass('link-first');
    }

    async function handleLinkClick(nodeId) {
        if (linkMode) {
            if (linkFirst === null) {
                linkFirst = nodeId;
                document.getElementById('mode-text').textContent =
                    `Link mode: N${nodeId} → click second node`;
                cy.nodes().removeClass('link-first');
                cy.getElementById(`n${nodeId}`).addClass('link-first');
            } else if (linkFirst === nodeId) {
                toast('Cannot link a node to itself', 'error');
            } else {
                const a = linkFirst, b = nodeId;
                exitLinkMode();
                await runMutation(async () => {
                    showLoading('Creating Link', `Connecting N${a} and N${b}...`);
                    toast(`Creating link N${a} ↔ N${b}...`, 'info');
                    await apiPost('/api/links', { node_a: a, node_b: b });
                    toast(`Link N${a} ↔ N${b} created`, 'success');
                    await syncAfterMutation();
                });
            }
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  ACTIONS
    // ═══════════════════════════════════════════════════════════

    const App = {
        async addNode() {
            return runMutation(async () => {
                showLoading('Creating Node', 'Starting ION container...');
                const node = await apiPost('/api/nodes');
                updateLoading('Syncing', 'Updating topology...');
                toast(`${node.name} created`, 'success');
                await syncAfterMutation();
            });
        },

        async deleteNode(nodeId) {
            return runMutation(async () => {
                showLoading('Deleting Node', `Removing N${nodeId}...`);
                await apiDelete(`/api/nodes/${nodeId}`);
                toast(`N${nodeId} deleted`, 'info');
                clearInspector();
                await syncAfterMutation();
            });
        },

        async deleteLink(linkId) {
            return runMutation(async () => {
                showLoading('Deleting Link', `Removing ${linkId}...`);
                await apiDelete(`/api/links/${linkId}`);
                toast(`Link ${linkId} deleted`, 'info');
                clearInspector();
                await syncAfterMutation();
            });
        },

        async disruptLink(linkId) {
            return runMutation(async () => {
                showLoading('Disrupting Link', `Applying netem on ${linkId}...`);
                await apiPost(`/api/links/${linkId}/disrupt`);
                toast(`Link ${linkId} disrupted`, 'info');
                await syncAfterMutation();
            });
        },

        async restoreLink(linkId) {
            return runMutation(async () => {
                showLoading('Restoring Link', `Removing netem on ${linkId}...`);
                await apiPost(`/api/links/${linkId}/restore`);
                toast(`Link ${linkId} restored`, 'success');
                await syncAfterMutation();
            });
        },

        async sendBundles() {
            const from = parseInt(document.getElementById('traffic-from').value);
            const to = parseInt(document.getElementById('traffic-to').value);
            const msg = document.getElementById('traffic-msg').value || 'hello';
            const count = parseInt(document.getElementById('traffic-count').value) || 1;
            if (!from || !to) { toast('Select source and destination', 'error'); return; }
            return runMutation(async () => {
                showLoading('Sending Bundles', `N${from} → N${to} (${count} bundle${count > 1 ? 's' : ''})...`);
                const r = await apiPost('/api/traffic/send', {
                    from_node: from, to_node: to, message: msg, count,
                });
                toast(`Sent ${r.sent} bundle(s): N${from} → N${to}`, 'success');
                setTimeout(fetchTopology, 2000);
            });
        },

        async cleanupAll() {
            if (!confirm('Remove ALL nodes and links?')) return;
            return runMutation(async () => {
                showLoading('Cleaning Up', 'Removing all containers and networks...');
                await apiPost('/api/cleanup');
                toast('Cleaned up', 'info');
                // Do NOT optimistically destroy terminals: syncTerminals
                // reconciles them from the fetched (server-truth) topology.
                await syncAfterMutation();
                clearInspector();
            });
        },

        async setExits() {
            return runMutation(async () => {
                showLoading('Setting Exits', 'Computing shortest-path routes...');
                await apiPost('/api/exits/set');
                toast('Exit routes set (shortest-path)', 'success');
            });
        },

        async clearExits() {
            return runMutation(async () => {
                showLoading('Clearing Exits', 'Removing all exit routes...');
                await apiPost('/api/exits/clear');
                toast('Exit routes cleared', 'success');
            });
        },

        async addExit(nodeId) {
            const destFirst = parseInt(document.getElementById('exit-dest-first').value);
            const destLast = parseInt(document.getElementById('exit-dest-last').value);
            const gateway = parseInt(document.getElementById('exit-gateway').value);
            if (!destFirst || !gateway) {
                toast('Select destination and gateway', 'error');
                return;
            }
            if (destLast < destFirst) {
                toast('Dest Last must be ≥ Dest First', 'error');
                return;
            }
            showLoading('Adding Exit', `Exit ${destFirst}-${destLast} → ipn:${gateway}.0`);
            try {
                await apiPost(`/api/nodes/${nodeId}/exits`, {
                    dest_first: destFirst,
                    dest_last: destLast,
                    gateway_id: gateway,
                });
                toast(`Exit added: ${destFirst}-${destLast} → ipn:${gateway}.0`, 'success');
                showNodeInspector(nodeId);
            } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
            finally { hideLoading(); }
        },

        async deleteExit(nodeId, destFirst, destLast) {
            showLoading('Deleting Exit', `Removing exit ${destFirst}-${destLast}...`);
            try {
                await apiDelete(`/api/nodes/${nodeId}/exits/${destFirst}/${destLast}`);
                toast(`Exit removed: ${destFirst}-${destLast}`, 'info');
                showNodeInspector(nodeId);
            } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
            finally { hideLoading(); }
        },

        async refreshExits(nodeId) {
            showNodeInspector(nodeId);
        },

        focusTerminal(nodeId) {
            activateTerminal(nodeId);
        },

        async viewIonLog(nodeId) {
            // Switch to this node's terminal
            activateTerminal(nodeId);
            const term = nodeTerminals[nodeId];
            if (!term) { toast('No terminal for this node', 'error'); return; }

            showLoading('Loading ion.log', `Fetching from N${nodeId}...`);
            try {
                const res = await fetch(`/api/nodes/${nodeId}/logs?tail=200`);
                if (!res.ok) throw new Error('Failed to fetch ion.log');
                const data = await res.json();
                const lines = data.logs ? data.logs.split('\n') : ['(empty)'];

                // Clear previous log dump and add fresh content
                term.panelEl.innerHTML = '';
                const sep = document.createElement('div');
                sep.className = 'log-info';
                sep.textContent = `── ion.log (last 200 lines) ──`;
                term.panelEl.appendChild(sep);

                for (const line of lines) {
                    const div = document.createElement('div');
                    div.textContent = line;
                    term.panelEl.appendChild(div);
                }
                term.panelEl.scrollTop = term.panelEl.scrollHeight;
                toast(`ion.log loaded for N${nodeId}`, 'success');
            } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
            finally { hideLoading(); }
        },

        async exportScenario() {
            if (topology.nodes.length === 0) {
                toast('No topology to export', 'error');
                return;
            }
            showLoading('Exporting Scenario', 'Capturing topology and exit routes...');
            try {
                // Capture current node positions from Cytoscape
                const positions = {};
                cy.nodes().forEach(n => {
                    const pos = n.position();
                    positions[n.id()] = { x: pos.x, y: pos.y };
                });

                const result = await apiPost('/api/scenarios/export', { positions });
                toast(`Exported: ${result.filename}`, 'success');

                // Always inject positions into the downloaded file (client-side)
                const scenarioWithPositions = { ...result.scenario, positions };
                const blob = new Blob(
                    [JSON.stringify(scenarioWithPositions, null, 2)],
                    { type: 'application/json' }
                );
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = result.filename;
                a.click();
                URL.revokeObjectURL(url);
            } catch (e) { toast(`Export failed: ${e.message}`, 'error'); }
            finally { hideLoading(); }
        },

        async exportBundle() {
            if (topology.nodes.length === 0) {
                toast('No topology to export', 'error');
                return;
            }
            showLoading('Exporting Bundle', 'Generating standalone scenario archive...');
            try {
                // Binary download: keep a raw fetch (blob success path), but
                // parse the error body defensively so a non-JSON 5xx surfaces
                // a real message instead of an "Unexpected token <" crash.
                const res = await fetch('/api/scenarios/export-bundle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({}),
                });
                if (!res.ok) {
                    const errText = await res.text();
                    let detail = `${res.status} ${res.statusText}`;
                    try { detail = JSON.parse(errText).detail || detail; } catch (_) {}
                    throw new Error(detail);
                }
                // Extract filename from Content-Disposition header
                const disposition = res.headers.get('Content-Disposition') || '';
                const match = disposition.match(/filename="?([^"]+)"?/);
                const filename = match ? match[1] : 'scenario.tar.gz';

                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                a.click();
                URL.revokeObjectURL(url);
                toast(`Bundle exported: ${filename}`, 'success');
            } catch (e) { toast(`Export bundle failed: ${e.message}`, 'error'); }
            finally { hideLoading(); }
        },

        importScenario() {
            document.getElementById('import-file-input').click();
        },

        async handleImportFile(file) {
            if (!file) return;
            if (!confirm('This will replace the current topology. Continue?')) return;
            if (mutationInFlight) { toast('Another operation is in progress…', 'info'); return; }

            mutationInFlight = true;
            let savedPositions = null;
            try {
                // Set the sync-pause flag INSIDE the try so an early throw can
                // never strand it true (which would freeze poll + WS forever).
                importInProgress = true;
                showLoading('Importing Scenario', 'Reading file...');

                const text = await file.text();
                try {
                    const parsed = JSON.parse(text);
                    savedPositions = parsed.positions || null;
                } catch (e) {}
                if (savedPositions) {
                    pendingPositions = { ...savedPositions };
                }

                showLoading('Importing Scenario', 'Creating nodes, links, and exit routes...');
                const formData = new FormData();
                formData.append('file', new Blob([text], { type: 'application/json' }), file.name);
                const data = await apiFetch('/api/scenarios/import', {
                    method: 'POST',
                    body: formData,
                });
                const exitMsg = data.exits_applied ? `, ${data.exits_applied} exits` : '';
                toast(`Imported: ${data.nodes_created} nodes, ${data.links_created} links${exitMsg}`, 'success');

                // Resume sync BEFORE fetching so the new state renders. Existing
                // terminals are reconciled by syncTerminals from server truth —
                // no optimistic destroy.
                importInProgress = false;
                await syncAfterMutation();

                if (savedPositions && cy) {
                    cy.nodes().forEach(n => {
                        const saved = savedPositions[n.id()];
                        if (saved) n.position({ x: saved.x, y: saved.y });
                    });
                }
                setTimeout(() => { pendingPositions = {}; }, 8000);
            } catch (e) {
                toast(`Import failed: ${e.message}`, 'error');
                console.error('[Import] Error:', e);
                // Reconcile UI to whatever the server actually has.
                try { await fetchTopology(); } catch (_) {}
            } finally {
                importInProgress = false;  // always resume sync
                mutationInFlight = false;
                hideLoading();
            }
        },
    };

    window.App = App;

    // ═══════════════════════════════════════════════════════════
    //  RESIZABLE PANELS
    // ═══════════════════════════════════════════════════════════

    function setupResize() {
        const root = document.documentElement;

        document.querySelectorAll('.resize-handle').forEach(handle => {
            const dir = handle.dataset.resize;
            const isHorizontal = handle.classList.contains('resize-h');

            handle.addEventListener('mousedown', (e) => {
                e.preventDefault();
                handle.classList.add('dragging');
                document.body.classList.add(isHorizontal ? 'resizing' : 'resizing-v');

                const startX = e.clientX;
                const startY = e.clientY;

                let startSize;
                if (dir === 'left') {
                    startSize = document.getElementById('left-sidebar').offsetWidth;
                } else if (dir === 'right') {
                    startSize = document.getElementById('right-sidebar').offsetWidth;
                } else if (dir === 'bottom') {
                    startSize = document.getElementById('terminal-bar').offsetHeight;
                }

                function onMove(ev) {
                    if (isHorizontal) {
                        const dx = ev.clientX - startX;
                        let newSize;
                        if (dir === 'left') {
                            newSize = Math.max(140, Math.min(600, startSize + dx));
                            root.style.setProperty('--left-width', newSize + 'px');
                        } else if (dir === 'right') {
                            newSize = Math.max(180, Math.min(600, startSize - dx));
                            root.style.setProperty('--right-width', newSize + 'px');
                        }
                    } else {
                        const dy = ev.clientY - startY;
                        let newSize = Math.max(60, Math.min(window.innerHeight * 0.6, startSize - dy));
                        root.style.setProperty('--bottom-height', newSize + 'px');
                    }
                    // Notify cytoscape of resize
                    if (cy) cy.resize();
                }

                function onUp() {
                    handle.classList.remove('dragging');
                    document.body.classList.remove('resizing', 'resizing-v');
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    if (cy) cy.resize();
                }

                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            });
        });
    }

    // ═══════════════════════════════════════════════════════════
    //  TOAST
    // ═══════════════════════════════════════════════════════════

    function toast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const el = document.createElement('div');
        el.className = `toast ${type}`;
        el.textContent = message;
        container.appendChild(el);
        setTimeout(() => el.remove(), 3500);
    }

    // ═══════════════════════════════════════════════════════════
    //  INITIALIZATION
    // ═══════════════════════════════════════════════════════════

    document.addEventListener('DOMContentLoaded', () => {
        initGraph();
        setupResize();
        showTerminalEmpty();

        // Toolbar
        document.getElementById('btn-add-node').addEventListener('click', App.addNode);
        document.getElementById('btn-link-mode').addEventListener('click', () => {
            if (linkMode) { exitLinkMode(); return; }
            if (topology.nodes.length < 2) { toast('Need at least 2 nodes', 'error'); return; }
            enterLinkMode();
        });
        document.getElementById('btn-cancel-mode').addEventListener('click', exitLinkMode);
        document.getElementById('btn-fit').addEventListener('click', () => cy.fit(undefined, 40));
        document.getElementById('btn-circle-layout').addEventListener('click', runCircleLayout);
        document.getElementById('btn-toggle-stats').addEventListener('click', () => {
            statsVisible = !statsVisible;
            const btn = document.getElementById('btn-toggle-stats');
            btn.querySelector('span:last-child').textContent = 'Stats';
            renderNodeStats();
        });

        // Sidebar
        document.getElementById('btn-send').addEventListener('click', App.sendBundles);
        document.getElementById('btn-cleanup').addEventListener('click', App.cleanupAll);
        document.getElementById('btn-set-exits').addEventListener('click', App.setExits);
        document.getElementById('btn-clear-exits').addEventListener('click', App.clearExits);
        document.getElementById('btn-export').addEventListener('click', App.exportScenario);
        document.getElementById('btn-export-bundle').addEventListener('click', App.exportBundle);
        document.getElementById('btn-import').addEventListener('click', App.importScenario);
        document.getElementById('import-file-input').addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                App.handleImportFile(e.target.files[0]);
                e.target.value = '';  // Reset so same file can be re-imported
            }
        });

        // Connection indicator + gate mutating buttons on connection health.
        ensureConnIndicator();
        ['btn-add-node', 'btn-link-mode', 'btn-send', 'btn-cleanup',
         'btn-set-exits', 'btn-clear-exits', 'btn-import'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.dataset.mutating = 'true';
        });

        // Data sync
        fetchTopology();
        startPolling();
        connectWsTopology();

        // Bundle stats
        startStatsPolling();
        attachStatsSync();

        console.log('[DTN-Manager] Ready');
    });
})();

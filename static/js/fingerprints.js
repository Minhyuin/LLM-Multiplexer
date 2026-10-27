/**
 * v1.9.2 模型指纹检测 — 门户前端。
 *
 * 职责边界：
 *   - 只做展示与交互；发起、计价、限流、去重全部由服务端判定。
 *   - 不持有任何密钥；key 只以服务端下发的脱敏标签出现在下拉框里。
 *   - 所有上游字符串（模型名、家族名、错误文本）一律通过 textContent 写入，
 *     从不拼 HTML，避免把服务端/上游内容当标记执行。
 *
 * 交互约定：
 *   - 模型列表默认没有检测按钮；点击某个模型条目才就地展开「检测指纹」操作，
 *     点击其它模型切换，点击空白处或按 Esc 收起。点击模型本身不发请求。
 *   - 点击「检测指纹」才创建任务；同一任务的重试沿用同一个幂等 UUID，避免重复计费。
 *   - 刷新页面时通过 fingerprint-state 恢复本人正在运行的任务。
 */
(function () {
    'use strict';

    var root = document.querySelector('[data-fp-root]');
    if (!root) { return; }

    var NA = '不可用';
    var USAGE_FALLBACK = '用量待同步/以上游账单为准';
    var POLL_MIN = 2500;                 // 服务端最快 2.5s 轮询一次
    var POLL_MAX = 20000;
    var POLL_HIDDEN = 10000;             // 页面不可见时降频
    var TERMINAL = {
        completed: true,
        partial: true,
        failed: true,
        cancelled: true,
        interrupted: true
    };
    var STATUS_LABEL = {
        queued: '排队中',
        running: '检测中',
        completed: '已完成',
        partial: '部分成功',
        failed: '失败',
        cancelled: '已取消',
        interrupted: '已中断'
    };
    var STATUS_TONE = {
        queued: 'fp-status-muted',
        running: 'fp-status-running',
        completed: 'fp-status-ok',
        partial: 'fp-status-warn',
        failed: 'fp-status-err',
        cancelled: 'fp-status-muted',
        interrupted: 'fp-status-warn'
    };
    var ERROR_TEXT = {
        no_key: '没有可用于检测的本人 key。',
        key_unusable: '所选 key 当前不可用（可能已撤销、过期或无法取到明文）。',
        read_only: '当前处于只读预览，不能发起检测。',
        quota_exhausted: '今日剩余轮次已用完，请明日再试。',
        daily_quota_exceeded: '今日剩余轮次已用完，请明日再试。',
        rate_limited: '操作过于频繁，请等待后再试。',
        cooldown: '两次检测之间需要间隔一段时间。',
        already_running: '已有正在进行的检测任务，请等待结束或先取消。',
        invalid_model: '该模型不支持检测，或当前渠道无法调用该模型。',
        queue_full: '检测队列已满，请稍后重试。',
        model_not_found: '模型列表中已找不到该模型，请刷新模型后重试。',
        forbidden: '无权执行该操作。',
        csrf_failed: '页面会话已过期，请刷新页面后重试。',
        network_error: '网络请求失败，请检查连接后重试。'
    };

    var cfg = {
        stateUrl: root.getAttribute('data-fp-state-url') || '',
        startUrl: root.getAttribute('data-fp-start-url') || '',
        readOnly: root.getAttribute('data-fp-read-only') === 'true'
    };

    var state = {
        enabled: true,
        readOnly: cfg.readOnly,
        keys: [],
        selectedKeyId: '',
        quota: null,
        activeRun: null,
        viewRun: null,
        history: [],
        pendingStart: null,
        startInFlight: false,
        pollTimer: null,
        pollDelay: POLL_MIN,
        pollFails: 0
    };

    var dom = {};

    /* ------------------------------------------------------------------ 基础工具 */

    function byId(id) { return document.getElementById(id); }

    function text(node, value) {
        if (node) { node.textContent = (value === null || value === undefined) ? '' : String(value); }
    }

    function show(node, visible) {
        if (node) { node.hidden = !visible; }
    }

    function pad2(value) { return (value < 10 ? '0' : '') + value; }

    function formatTime(value) {
        if (!value) { return NA; }
        var date = new Date(value);
        if (isNaN(date.getTime())) { return NA; }
        return pad2(date.getMonth() + 1) + '-' + pad2(date.getDate()) + ' ' +
            pad2(date.getHours()) + ':' + pad2(date.getMinutes()) + ':' + pad2(date.getSeconds());
    }

    function formatClock(value) {
        if (!value) { return ''; }
        var date = new Date(value);
        if (isNaN(date.getTime())) { return ''; }
        return pad2(date.getHours()) + ':' + pad2(date.getMinutes()) + ':' + pad2(date.getSeconds());
    }

    function isNumber(value) { return typeof value === 'number' && isFinite(value); }

    function scalarOrNA(value) {
        if (typeof value === 'string' && value !== '') { return value; }
        if (isNumber(value)) { return String(value); }
        return NA;
    }

    function trimZeros(value) {
        var out = value;
        if (out.indexOf('.') >= 0) {
            out = out.replace(/0+$/, '');
            out = out.replace(/\.$/, '');
        }
        return out || '0';
    }

    function formatScore(value) {
        if (!isNumber(value)) { return NA; }
        return trimZeros(value.toFixed(4));
    }

    function formatProbability(value) {
        if (!isNumber(value)) { return NA; }
        // 契约里 probability 是 [0,1] 的比例；超过 1 视为服务端已给百分比。
        if (value >= 0 && value <= 1) { return trimZeros((value * 100).toFixed(1)) + '%'; }
        return trimZeros(value.toFixed(2));
    }

    function cookie(name) {
        var parts = ('; ' + document.cookie).split('; ' + name + '=');
        if (parts.length === 2) { return parts.pop().split(';').shift(); }
        return '';
    }

    function uuid() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            try { return window.crypto.randomUUID(); } catch (err) { /* 回退到手工生成 */ }
        }
        var bytes = [];
        var i;
        if (window.crypto && typeof window.crypto.getRandomValues === 'function') {
            var buffer = new Uint8Array(16);
            window.crypto.getRandomValues(buffer);
            for (i = 0; i < buffer.length; i += 1) { bytes.push(buffer[i]); }
        } else {
            for (i = 0; i < 16; i += 1) { bytes.push(Math.floor(Math.random() * 256)); }
        }
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        var hex = '';
        for (i = 0; i < bytes.length; i += 1) { hex += (bytes[i] + 0x100).toString(16).slice(1); }
        return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' +
            hex.slice(16, 20) + '-' + hex.slice(20);
    }

    function statusText(run) {
        if (!run) { return NA; }
        if (typeof run.status_label === 'string' && run.status_label) { return run.status_label; }
        return STATUS_LABEL[run.status] || scalarOrNA(run.status);
    }

    function statusTone(status) { return STATUS_TONE[status] || 'fp-status-muted'; }

    function isTerminal(status) { return !!TERMINAL[status]; }

    function requestJson(url, options) {
        options = options || {};
        var method = options.method || 'GET';
        var init = {
            method: method,
            credentials: 'same-origin',
            cache: 'no-store',
            redirect: 'error',
            headers: { 'Accept': 'application/json' }
        };
        if (method !== 'GET') {
            init.headers['Content-Type'] = 'application/json';
            init.headers['X-CSRFToken'] = cookie('csrftoken');
            init.body = JSON.stringify(options.body || {});
        }
        return fetch(url, init).then(function (response) {
            return response.text().then(function (raw) {
                var payload = null;
                try { payload = raw ? JSON.parse(raw) : null; } catch (err) { payload = null; }
                if (!response.ok) { throw normalizeError(payload, response.status); }
                if (!payload || typeof payload !== 'object') {
                    throw {
                        code: 'bad_response',
                        message: '服务器返回了无法解析的响应。',
                        status: response.status,
                        retryable: false
                    };
                }
                if (payload.error) { throw normalizeError(payload, response.status); }
                return payload;
            });
        }, function () {
            throw {
                code: 'network_error',
                message: ERROR_TEXT.network_error,
                status: 0,
                retryable: true
            };
        });
    }

    function normalizeError(payload, status) {
        var detail = (payload && payload.error && typeof payload.error === 'object') ? payload.error : {};
        var code = typeof detail.code === 'string' ? detail.code : '';
        var message = typeof detail.message === 'string' ? detail.message : '';
        return {
            code: code || 'http_' + status,
            message: message,
            status: status,
            retryable: status === 0 || status === 408 || status === 425 || status === 429 || status >= 500
        };
    }

    function errorText(err) {
        if (!err) { return '操作失败，请稍后重试。'; }
        if (ERROR_TEXT[err.code]) { return ERROR_TEXT[err.code]; }
        if (err.message) { return err.message; }
        return '操作失败，请稍后重试。';
    }

    /* -------------------------------------------------------------- 提示区与配额 */

    function setNotice(message, tone, retryable) {
        if (!dom.notice) { return; }
        text(dom.noticeText, message || '');
        dom.notice.className = 'fp-notice fp-notice-' + (tone || 'info');
        show(dom.notice, !!(message || ''));
        show(dom.noticeRetry, !!(retryable && state.pendingStart && !state.startInFlight));
        syncCard();
    }

    function clearNotice() { setNotice('', 'info', false); }

    function refreshRetryVisibility() {
        if (!dom.noticeRetry) { return; }
        show(dom.noticeRetry, !!(state.pendingStart && !state.startInFlight));
    }

    function renderQuota() {
        var quota = state.quota || {};
        var limit = isNumber(quota.limit) ? quota.limit : null;
        var remaining = isNumber(quota.remaining) ? quota.remaining : null;
        var label;
        if (limit === null || remaining === null) {
            label = '今日剩余 — 轮';
        } else {
            label = '今日剩余 ' + remaining + '/' + limit + ' 轮';
        }
        text(dom.quotaText, label);

        var next = formatClock(quota.next_allowed_at);
        if (remaining === 0 && next) {
            text(dom.quotaNext, '今日剩余轮次已用完，下次可检测时间约 ' + next + '（按上海自然日重置）。');
            show(dom.quotaNext, true);
        } else if (remaining === 0) {
            text(dom.quotaNext, '今日剩余轮次已用完（按上海自然日重置）。');
            show(dom.quotaNext, true);
        } else if (next) {
            text(dom.quotaNext, '下次可发起时间约 ' + next + '。');
            show(dom.quotaNext, true);
        } else {
            text(dom.quotaNext, '');
            show(dom.quotaNext, false);
        }
    }

    function usableKeys() {
        return state.keys.filter(function (key) {
            return key && key.id !== null && key.id !== undefined && String(key.id) !== '';
        });
    }

    function selectedKey() {
        var keys = usableKeys();
        for (var i = 0; i < keys.length; i += 1) {
            if (String(keys[i].id) === String(state.selectedKeyId)) { return keys[i]; }
        }
        return null;
    }

    function keyLabel(key) {
        if (!key) { return ''; }
        if (typeof key.masked === 'string' && key.masked) { return key.masked; }
        if (typeof key.name === 'string' && key.name) { return key.name; }
        return '未命名 key';
    }

    function renderKeys() {
        var select = dom.keySelect;
        if (!select) { return; }
        var keys = usableKeys();
        while (select.firstChild) { select.removeChild(select.firstChild); }

        if (!keys.length) {
            var empty = document.createElement('option');
            empty.value = '';
            empty.textContent = state.readOnly ? '只读预览：不读取可用 key' : '没有可用的本人 key';
            select.appendChild(empty);
            select.disabled = true;
            text(dom.keyNote, state.readOnly
                ? '当前为账户视角只读预览，不会发起任何检测或计费请求。'
                : '没有可用于检测的本人 key，无法发起检测。');
            show(dom.keyNote, true);
            return;
        }

        keys.forEach(function (key) {
            var option = document.createElement('option');
            option.value = String(key.id);
            option.textContent = keyLabel(key);
            if (String(key.id) === String(state.selectedKeyId)) { option.selected = true; }
            select.appendChild(option);
        });
        // 只暴露脱敏标签；多个 key 时允许手动切换，只读预览下整个选择器禁用。
        select.disabled = keys.length < 2 || state.readOnly;
        text(dom.keyNote, state.readOnly ? '当前为账户视角只读预览，不会发起任何检测或计费请求。' : '');
        show(dom.keyNote, state.readOnly);
    }

    /* -------------------------------------------------------------- 是否允许发起 */

    function startGate() {
        if (!state.enabled) { return { ok: false, reason: '模型指纹检测功能未开启。' }; }
        if (state.readOnly) { return { ok: false, reason: '当前处于只读预览，不能发起检测。' }; }
        if (!usableKeys().length) { return { ok: false, reason: '没有可用于检测的本人 key。' }; }
        if (!selectedKey()) { return { ok: false, reason: '请选择用于检测的本人 key。' }; }
        var quota = state.quota || {};
        if (isNumber(quota.remaining) && quota.remaining <= 0) {
            return { ok: false, reason: '今日剩余轮次已用完，请明日再试。' };
        }
        if (state.activeRun && !isTerminal(state.activeRun.status)) {
            return { ok: false, reason: '已有正在进行的检测任务，请等待结束或先取消。' };
        }
        return { ok: true, reason: '' };
    }

    /**
     * 单个模型条目的可发起判定：先看服务端给的显式标记
     * （model.fingerprint_supported / fingerprint_reason），只有明确为 0 时禁用，
     * 不看模型名做任何猜测；其余情况走整体 gate（功能开关/只读/key/额度/并发）。
     */
    function actionBlock(action) {
        var chip = chipForModel(action.getAttribute('data-fp-action') || '');
        if (chip && chip.getAttribute('data-fp-supported') === '0') {
            var reason = chip.getAttribute('data-fp-reason') || '';
            return { blocked: true, reason: reason || '该模型当前不支持指纹检测。' };
        }
        var gate = startGate();
        return { blocked: !gate.ok, reason: gate.ok ? '' : gate.reason };
    }

    function applyGateToActions() {
        var actions = document.querySelectorAll('[data-fp-action]');
        for (var i = 0; i < actions.length; i += 1) {
            var action = actions[i];
            var blocked = actionBlock(action);
            var button = action.querySelector('[data-fp-start]');
            if (button) {
                button.disabled = blocked.blocked;
                if (blocked.blocked) {
                    button.setAttribute('title', blocked.reason);
                } else {
                    button.removeAttribute('title');
                }
            }
            var note = action.querySelector('[data-fp-note]');
            if (note) {
                text(note, blocked.blocked ? blocked.reason : '');
                show(note, blocked.blocked);
            }
        }
    }

    /* -------------------------------------------------------- 模型列表操作（事件委托） */

    var actionSeq = 0;

    function chipForModel(modelId) {
        var chips = document.querySelectorAll('.model-chip[data-fp-model]');
        for (var i = 0; i < chips.length; i += 1) {
            if (chips[i].getAttribute('data-fp-model') === modelId) { return chips[i]; }
        }
        return null;
    }

    function actionForChip(chip) {
        var parent = chip.parentNode;
        if (!parent || !parent.classList || !parent.classList.contains('model-chip-cell')) { return null; }
        var found = parent.querySelector('[data-fp-action]');
        return (found && found.getAttribute('data-fp-action') === (chip.getAttribute('data-fp-model') || '')) ? found : null;
    }

    /**
     * 模型条目是 .model-chip-grid 里的 flex item。把条目包进一个单元格，
     * 展开的操作就落在该条目正下方，而不会挤到旁边模型的同一行里。
     */
    function ensureCell(chip) {
        var parent = chip.parentNode;
        if (parent && parent.classList && parent.classList.contains('model-chip-cell')) { return parent; }
        var cell = document.createElement('span');
        cell.className = 'model-chip-cell';
        if (parent) {
            parent.insertBefore(cell, chip);
        }
        cell.appendChild(chip);
        return cell;
    }

    function buildAction(chip) {
        var modelId = chip.getAttribute('data-fp-model') || '';
        actionSeq += 1;
        var action = document.createElement('span');
        action.className = 'model-chip-action';
        action.setAttribute('data-fp-action', modelId);
        action.id = 'fp-action-' + actionSeq;
        action.hidden = true;

        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'fp-detect-btn';
        button.setAttribute('data-fp-start', modelId);
        button.textContent = '检测指纹';

        var note = document.createElement('span');
        note.className = 'fp-action-note';
        note.setAttribute('data-fp-note', '');
        note.hidden = true;

        action.appendChild(button);
        action.appendChild(note);

        var cell = ensureCell(chip);
        cell.appendChild(action);
        chip.setAttribute('aria-controls', action.id);
        return action;
    }

    /**
     * 模型列表会被「刷新模型」整体替换成新的 HTML，所以这里每次替换后都重新
     * 升级一遍：只补语义属性和操作节点，不动原有结构与样式。
     */
    function upgradeChips() {
        var chips = document.querySelectorAll('.model-chip[data-fp-model]');
        for (var i = 0; i < chips.length; i += 1) {
            var chip = chips[i];
            if (chip.getAttribute('data-fp-ready') === '1') { continue; }
            chip.setAttribute('data-fp-ready', '1');
            chip.setAttribute('role', 'button');
            chip.setAttribute('tabindex', '0');
            chip.setAttribute('aria-haspopup', 'true');
            chip.setAttribute('aria-expanded', 'false');
            if (chip.getAttribute('data-fp-supported') === '0') {
                // 服务端明确标注不支持（非文本模型 / 服务端策略不允许）：只标样式，
                // 展开后显示原因，不给可点击的发起按钮。这里不看模型名做任何猜测。
                chip.classList.add('is-unsupported');
                var unsupported = chip.getAttribute('data-fp-reason') || '';
                if (unsupported) { chip.setAttribute('title', unsupported); }
            }
            if (!actionForChip(chip)) { buildAction(chip); }
        }
        applyGateToActions();
    }

    function closeChip(chip) {
        var action = actionForChip(chip);
        if (action) { action.hidden = true; }
        chip.classList.remove('is-open');
        chip.setAttribute('aria-expanded', 'false');
    }

    function closeAllChips() {
        var chips = document.querySelectorAll('.model-chip[data-fp-model]');
        for (var i = 0; i < chips.length; i += 1) { closeChip(chips[i]); }
    }

    function openChip(chip) {
        closeAllChips();
        var action = actionForChip(chip) || buildAction(chip);
        action.hidden = false;
        chip.classList.add('is-open');
        chip.setAttribute('aria-expanded', 'true');
        applyGateToActions();
        var button = action.querySelector('[data-fp-start]');
        if (button && !button.disabled && button.focus) {
            // 焦点跟随展开的操作，键盘用户可以立刻继续按 Enter。
            button.focus();
        }
    }

    function toggleChip(chip) {
        if (chip.getAttribute('aria-expanded') === 'true') {
            closeChip(chip);
        } else {
            openChip(chip);
        }
    }

    /* ------------------------------------------------------------------ 说明气泡 */

    function toggleInfoHint() {
        if (!dom.infoHint) { return; }
        var next = dom.infoHint.hidden;
        show(dom.infoHint, next);
        if (dom.infoBtn) { dom.infoBtn.setAttribute('aria-expanded', next ? 'true' : 'false'); }
    }

    function closeInfoHint() {
        if (!dom.infoHint || dom.infoHint.hidden) { return; }
        show(dom.infoHint, false);
        if (dom.infoBtn) { dom.infoBtn.setAttribute('aria-expanded', 'false'); }
    }

    /* ------------------------------------------------------------------ 结果渲染 */

    function setStatusPill(run) {
        if (!dom.runStatus) { return; }
        dom.runStatus.className = 'fp-status ' + statusTone(run ? run.status : '');
        text(dom.runStatus, statusText(run));
    }

    function renderProgress(run) {
        var total = isNumber(run.total_samples) ? run.total_samples : 3;
        var done = isNumber(run.completed_samples) ? run.completed_samples : null;
        var valid = isNumber(run.valid_samples) ? run.valid_samples : null;
        var parts = [];

        if (run.status === 'queued') {
            parts.push('排队中，最长等待约 3 分钟');
        } else if (isTerminal(run.status)) {
            parts.push('已完成 ' + (done === null ? '—' : done) + '/' + total + ' 条请求');
        } else {
            var ordinal = done === null ? null : Math.min(done + 1, total);
            parts.push(ordinal === null ? '检测进行中' : ('检测第 ' + ordinal + '/' + total + ' 条'));
        }
        parts.push('有效样本 ' + (valid === null ? '—' : valid) + ' 条');
        if (run.cancel_requested && !isTerminal(run.status)) { parts.push('已请求取消，等待在途请求结束'); }
        text(dom.progressText, parts.join(' · '));

        if (dom.progressFill) {
            if (done === null) {
                dom.progressFill.style.width = '0%';
                dom.progressFill.className = 'fp-progress-fill fp-progress-indeterminate';
            } else {
                var percent = total > 0 ? Math.round((done / total) * 100) : 0;
                dom.progressFill.style.width = Math.max(0, Math.min(100, percent)) + '%';
                dom.progressFill.className = 'fp-progress-fill';
            }
        }
    }

    function renderCandidates(result) {
        var list = Array.isArray(result.candidates) ? result.candidates.slice(0, 5) : [];
        var body = dom.candidatesBody;
        if (body) {
            while (body.firstChild) { body.removeChild(body.firstChild); }
        }
        if (!list.length) {
            show(dom.candidates, true);
            show(dom.candidatesEmpty, true);
            var wrap = dom.candidates ? dom.candidates.querySelector('.fp-table-wrap') : null;
            show(wrap, false);
            return;
        }
        show(dom.candidates, true);
        show(dom.candidatesEmpty, false);
        var tableWrap = dom.candidates ? dom.candidates.querySelector('.fp-table-wrap') : null;
        show(tableWrap, true);
        list.forEach(function (candidate) {
            if (!body) { return; }
            var row = document.createElement('tr');
            var name = document.createElement('td');
            name.className = 'fp-candidate-name';
            text(name, scalarOrNA(candidate ? candidate.model : null));
            var score = document.createElement('td');
            score.className = 'fp-candidate-score';
            text(score, formatScore(candidate ? candidate.score : null));
            var probability = document.createElement('td');
            probability.className = 'fp-candidate-probability';
            text(probability, formatProbability(candidate ? candidate.probability : null));
            row.appendChild(name);
            row.appendChild(score);
            row.appendChild(probability);
            body.appendChild(row);
        });
    }

    function sampleMeta(sample) {
        var parts = [];
        if (typeof sample.valid === 'boolean') { parts.push(sample.valid ? '有效' : '无效'); }
        if (isNumber(sample.number_count)) { parts.push('整数 ' + sample.number_count + ' 个'); }
        if (isNumber(sample.duration_ms)) { parts.push((sample.duration_ms / 1000).toFixed(1) + 's'); }
        if (sample.finish_reason) { parts.push('结束原因 ' + scalarOrNA(sample.finish_reason)); }
        if (sample.response_model) { parts.push('返回模型 ' + sample.response_model); }
        return parts.join(' · ') || NA;
    }

    function renderSamples(samples) {
        var list = dom.samples;
        if (!list) { return; }
        while (list.firstChild) { list.removeChild(list.firstChild); }
        var items = Array.isArray(samples) ? samples : [];
        show(dom.samplesWrap, items.length > 0);
        if (!items.length) { return; }
        items.forEach(function (sample) {
            if (!sample) { return; }
            var item = document.createElement('li');
            var tone = sample.valid ? 'fp-sample-ok' : 'fp-sample-warn';
            if (sample.status === 'failed' || sample.status === 'interrupted') { tone = 'fp-sample-err'; }
            item.className = 'fp-sample ' + tone;

            var head = document.createElement('span');
            head.className = 'fp-sample-head';
            var ordinal = isNumber(sample.ordinal) ? sample.ordinal : null;
            text(head, (ordinal === null ? '样本' : '第 ' + ordinal + ' 条') +
                (sample.status ? ' · ' + scalarOrNA(sample.status) : ''));

            var meta = document.createElement('span');
            meta.className = 'fp-sample-meta';
            text(meta, sampleMeta(sample));

            item.appendChild(head);
            item.appendChild(meta);
            if (sample.error_message) {
                var error = document.createElement('span');
                error.className = 'fp-sample-error';
                text(error, sample.error_message);
                item.appendChild(error);
            }
            list.appendChild(item);
        });
    }

    function usageText(usage) {
        if (!usage || typeof usage !== 'object') { return USAGE_FALLBACK; }
        var parts = [];
        if (isNumber(usage.input_tokens)) { parts.push('输入 ' + usage.input_tokens); }
        if (isNumber(usage.output_tokens)) { parts.push('输出 ' + usage.output_tokens); }
        if (isNumber(usage.cached_tokens)) { parts.push('缓存 ' + usage.cached_tokens); }
        if (isNumber(usage.reasoning_tokens)) { parts.push('推理 ' + usage.reasoning_tokens); }
        if (!parts.length) { return USAGE_FALLBACK; }
        var out = parts.join(' · ') + ' tokens';
        if (usage.complete !== true) { out += '（部分字段未返回，以上游账单为准）'; }
        return out;
    }

    function parametersText(parameters) {
        if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) { return ''; }
        var parts = [];
        Object.keys(parameters).forEach(function (name) {
            var value = parameters[name];
            if (value === null || value === undefined || value === '') { return; }
            if (typeof value === 'object') {
                try { parts.push(name + '=' + JSON.stringify(value)); } catch (err) { return; }
                return;
            }
            parts.push(name + '=' + String(value));
        });
        return parts.join(' · ');
    }

    function durationText(run) {
        var samples = Array.isArray(run.samples) ? run.samples : [];
        var total = 0;
        var count = 0;
        samples.forEach(function (sample) {
            if (sample && isNumber(sample.duration_ms)) { total += sample.duration_ms; count += 1; }
        });
        if (count && total > 0) { return (total / 1000).toFixed(1) + ' 秒（' + count + ' 条请求合计）'; }
        if (run.started_at && run.finished_at) {
            var start = new Date(run.started_at).getTime();
            var end = new Date(run.finished_at).getTime();
            if (!isNaN(start) && !isNaN(end) && end >= start) {
                return ((end - start) / 1000).toFixed(1) + ' 秒（发起至结束）';
            }
        }
        return '';
    }

    function renderMeta(run) {
        text(dom.kvProtocol, scalarOrNA(run.protocol));
        text(dom.kvKey, scalarOrNA(run.key_label));
        text(dom.kvUpstream, scalarOrNA(run.upstream_commit));
        text(dom.kvBank, run.bank_built_at ? formatTime(run.bank_built_at) : NA);
        var params = parametersText(run.parameters);
        text(dom.kvParams, params);
        show(dom.paramsRow, !!params);
        var duration = durationText(run);
        text(dom.kvDuration, duration);
        show(dom.durationRow, !!duration);
        text(dom.usage, usageText(run.usage));
    }

    function renderRunError(run) {
        var hasError = !!(run.error_code || run.error_message);
        if (!hasError) {
            text(dom.runError, '');
            show(dom.runError, false);
            return;
        }
        var message = run.error_message || ERROR_TEXT[run.error_code] || '';
        var detail = message || '检测未能完成。';
        if (run.error_code) { detail = detail + '（' + run.error_code + '）'; }
        text(dom.runError, detail);
        show(dom.runError, true);
    }

    function renderActions(run) {
        var active = run && !isTerminal(run.status);
        show(dom.runCancel, !!active);
        if (dom.runCancel) {
            dom.runCancel.disabled = !!run.cancel_requested;
            dom.runCancel.title = run.cancel_requested ? '已请求取消' : '取消尚未发出的请求；已发出的调用仍可能收费';
        }
        var canAgain = !!run && isTerminal(run.status) && !run.cancel_requested;
        show(dom.runAgain, canAgain);
        if (dom.runAgain) { dom.runAgain.setAttribute('data-fp-model-for', run.model_id || ''); }
        var viewingOther = !!(state.activeRun && state.viewRun && state.activeRun.id !== state.viewRun.id &&
            !isTerminal(state.activeRun.status));
        show(dom.runBack, viewingOther);
    }

    /**
     * 任务结束时把它并进「最近 24 小时」列表：服务端刷新是异步的，
     * 这里先在本地补一条，不让刚跑完的结果在一分钟内无处可查。
     * 同一任务只保留最新快照，点开历史回看也不会打乱原来的顺序。
     */
    function rememberHistory(run) {
        if (!run || !run.id || !Array.isArray(state.history)) { return; }
        for (var i = 0; i < state.history.length; i += 1) {
            if (state.history[i] && String(state.history[i].id) === String(run.id)) {
                state.history[i] = run;
                renderHistory();
                return;
            }
        }
        state.history.unshift(run);
        renderHistory();
    }

    function renderRun(run) {
        if (!run || !run.id) { return; }
        state.viewRun = run;
        show(dom.result, true);
        syncCard();

        text(dom.runModel, scalarOrNA(run.model_id));
        text(dom.runTime, run.created_at ? '发起于 ' + formatTime(run.created_at) : NA);
        setStatusPill(run);
        renderProgress(run);
        renderRunError(run);

        var result = (run.result && typeof run.result === 'object') ? run.result : {};
        var hasResult = isTerminal(run.status) && (result.candidates || result.family || result.verdict);
        show(dom.verdict, !!hasResult);
        text(dom.runFamily, scalarOrNA(result.family));
        text(dom.runVerdict, scalarOrNA(result.verdict));
        renderCandidates(hasResult ? result : { candidates: [] });
        if (!hasResult) { show(dom.candidates, false); }
        renderSamples(run.samples);
        renderMeta(run);
        renderActions(run);
        applyGateToActions();
        if (isTerminal(run.status)) { rememberHistory(run); }
    }

    /**
     * 下方结果卡片默认隐藏：只有提示、结果或历史记录真的存在时才显示，
     * 一次检测都没跑过的页面就只有列表和上方那行入口说明。
     */
    function syncCard() {
        if (!dom.card) { return; }
        var hasContent = !!(dom.notice && !dom.notice.hidden) ||
            !!(dom.result && !dom.result.hidden) ||
            !!(dom.history && !dom.history.hidden);
        show(dom.card, hasContent);
    }

    function renderHistory() {
        var list = dom.historyList;
        if (!list || !dom.history) { return; }
        while (list.firstChild) { list.removeChild(list.firstChild); }
        var runs = Array.isArray(state.history) ? state.history : [];
        if (!runs.length) {
            show(dom.history, false);
            text(dom.historyCount, '');
            syncCard();
            return;
        }
        show(dom.history, true);
        text(dom.historyCount, '（' + runs.length + ' 条）');
        runs.forEach(function (run, index) {
            if (!run) { return; }
            var item = document.createElement('li');
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'fp-history-item';
            button.setAttribute('data-fp-history', String(index));

            var pill = document.createElement('span');
            pill.className = 'fp-status fp-status-sm ' + statusTone(run.status);
            text(pill, statusText(run));

            var model = document.createElement('span');
            model.className = 'fp-history-model';
            text(model, scalarOrNA(run.model_id));

            var when = document.createElement('span');
            when.className = 'fp-history-time';
            text(when, formatTime(run.finished_at || run.created_at));

            button.appendChild(pill);
            button.appendChild(model);
            button.appendChild(when);
            item.appendChild(button);
            list.appendChild(item);
        });
        syncCard();
    }

    /* ------------------------------------------------------------------ 状态加载 */

    function applyState(data) {
        state.enabled = data.enabled !== false;
        state.readOnly = cfg.readOnly || data.read_only === true;
        state.quota = (data.quota && typeof data.quota === 'object') ? data.quota : null;
        state.keys = Array.isArray(data.keys) ? data.keys : [];
        state.history = Array.isArray(data.history) ? data.history : [];

        var keys = usableKeys();
        var keep = false;
        for (var i = 0; i < keys.length; i += 1) {
            if (String(keys[i].id) === String(state.selectedKeyId)) { keep = true; break; }
        }
        if (!keep) { state.selectedKeyId = keys.length ? String(keys[0].id) : ''; }

        renderQuota();
        renderKeys();
        renderHistory();

        var active = (data.active && data.active.id) ? data.active : null;
        if (active) {
            state.activeRun = active;
            if (!state.viewRun || state.viewRun.id === active.id) {
                renderRun(active);
            } else {
                renderActions(state.viewRun);
            }
            if (!isTerminal(active.status)) {
                state.pollDelay = POLL_MIN;
                state.pollFails = 0;
                schedulePoll(1200);
            } else {
                stopPoll();
            }
        } else {
            // 状态列表里没有进行中的任务，但本地还跟随着一个未结束的任务时，
            // 不能就此停掉轮询——那会让界面永久停在「检测中」。改为按任务自己
            // 的状态地址再确认一次，由那一次结果决定是结束还是继续。
            var tracked = (state.activeRun && state.activeRun.id && !isTerminal(state.activeRun.status))
                ? state.activeRun
                : null;
            if (tracked && tracked.status_url) {
                state.pollDelay = POLL_MIN;
                schedulePoll(800);
            } else {
                stopPoll();
                state.activeRun = null;
            }
            if (state.viewRun) { renderActions(state.viewRun); }
        }
        applyGateToActions();
    }

    function loadState(options) {
        options = options || {};
        if (!cfg.stateUrl) { return Promise.resolve(null); }
        return requestJson(cfg.stateUrl).then(function (data) {
            applyState(data || {});
            return data;
        }).catch(function (err) {
            if (!options.quiet) { setNotice(errorText(err), 'err', false); }
            return null;
        });
    }

    /* ------------------------------------------------------------------ 轮询 */

    function stopPoll() {
        if (state.pollTimer) {
            window.clearTimeout(state.pollTimer);
            state.pollTimer = null;
        }
    }

    function schedulePoll(delay) {
        stopPoll();
        var wait = delay;
        if (document.visibilityState === 'hidden') {
            wait = Math.max(delay || 0, POLL_HIDDEN);
        }
        state.pollTimer = window.setTimeout(pollOnce, Math.max(0, wait));
    }

    function pollOnce() {
        state.pollTimer = null;
        var run = state.activeRun;
        if (!run || !run.id) { return; }
        if (isTerminal(run.status)) { stopPoll(); return; }
        if (!run.status_url) {
            setNotice('服务端未提供任务状态地址，无法继续更新进度。', 'warn', false);
            return;
        }
        requestJson(run.status_url).then(function (data) {
            state.pollFails = 0;
            if (data && data.quota) { state.quota = data.quota; renderQuota(); }
            var next = (data && data.run) ? data.run : run;
            state.activeRun = next;
            if (state.viewRun && state.viewRun.id === next.id) {
                renderRun(next);
            } else {
                renderActions(state.viewRun || next);
            }
            if (isTerminal(next.status)) {
                stopPoll();
                loadState({ quiet: true });
                return;
            }
            state.pollDelay = Math.min(Math.max(state.pollDelay, POLL_MIN) * 1.35, POLL_MAX);
            schedulePoll(state.pollDelay);
        }).catch(function (err) {
            state.pollFails += 1;
            // 明确的客户端错误（无权限/已删除/CSRF）不再重试，避免无意义轮询。
            if (err && err.status >= 400 && err.status < 500 && err.status !== 408 && err.status !== 429) {
                stopPoll();
                setNotice(errorText(err), 'err', false);
                return;
            }
            var wait = Math.min(Math.max(state.pollDelay, POLL_MIN) * Math.pow(1.6, Math.min(state.pollFails, 4)), POLL_MAX);
            state.pollDelay = wait;
            schedulePoll(wait);
        });
    }

    /* ------------------------------------------------------------------ 发起/取消 */

    function setDetectBusy(modelId) {
        var buttons = document.querySelectorAll('[data-fp-start]');
        for (var i = 0; i < buttons.length; i += 1) {
            var button = buttons[i];
            var isTarget = button.getAttribute('data-fp-start') === modelId;
            button.disabled = !!modelId;
            text(button, (modelId && isTarget) ? '发起中…' : '检测指纹');
        }
    }

    function startDetection(modelId) {
        if (!modelId || state.startInFlight) { return; }
        var chip = chipForModel(modelId);
        if (chip && chip.getAttribute('data-fp-supported') === '0') {
            setNotice(chip.getAttribute('data-fp-reason') || '该模型当前不支持指纹检测。', 'warn', false);
            return;
        }
        if (!cfg.startUrl) {
            setNotice('缺少发起接口配置，无法开始检测。', 'err', false);
            return;
        }
        var gate = startGate();
        if (!gate.ok) {
            setNotice(gate.reason, 'warn', false);
            return;
        }
        // 同一次「发起」的重试复用同一个幂等 UUID；换模型才会重新生成。
        if (!state.pendingStart || state.pendingStart.modelId !== modelId) {
            state.pendingStart = { modelId: modelId, idempotencyKey: uuid() };
        }
        var payload = {
            key_id: state.selectedKeyId,
            model_id: modelId,
            idempotency_key: state.pendingStart.idempotencyKey
        };
        state.startInFlight = true;
        setDetectBusy(modelId);
        refreshRetryVisibility();

        requestJson(cfg.startUrl, { method: 'POST', body: payload }).then(function (data) {
            state.pendingStart = null;
            state.startInFlight = false;
            setDetectBusy('');
            refreshRetryVisibility();
            applyGateToActions();
            if (data && data.quota) { state.quota = data.quota; renderQuota(); }
            if (data && data.run) {
                state.activeRun = data.run;
                renderRun(data.run);
                if (data.reused) {
                    setNotice('已恢复同一个检测任务，不会重复发起请求。', 'info', false);
                } else {
                    clearNotice();
                }
                if (!isTerminal(data.run.status)) {
                    state.pollDelay = POLL_MIN;
                    state.pollFails = 0;
                    schedulePoll(1200);
                }
            }
            closeAllChips();
            loadState({ quiet: true });
        }).catch(function (err) {
            state.startInFlight = false;
            setDetectBusy('');
            applyGateToActions();
            if (err && err.status >= 400 && err.status < 500 && err.status !== 408 && err.status !== 429) {
                // 服务端已明确拒绝：这个幂等标识没有意义了，下次发起重新生成。
                state.pendingStart = null;
                setNotice(errorText(err), 'err', false);
                refreshRetryVisibility();
                return;
            }
            setNotice(errorText(err), 'err', true);
            refreshRetryVisibility();
        });
    }

    function retryPendingStart() {
        if (!state.pendingStart || state.startInFlight) { return; }
        startDetection(state.pendingStart.modelId);
    }

    function cancelRun() {
        var run = state.viewRun && !isTerminal(state.viewRun.status) ? state.viewRun : state.activeRun;
        if (!run || !run.id) { return; }
        if (!run.cancel_url) {
            setNotice('服务端未提供取消地址，无法取消该任务。', 'warn', false);
            return;
        }
        if (dom.runCancel) { dom.runCancel.disabled = true; }
        requestJson(run.cancel_url, { method: 'POST', body: {} }).then(function (data) {
            if (data && data.quota) { state.quota = data.quota; renderQuota(); }
            var next = (data && data.run) ? data.run : run;
            state.activeRun = next;
            if (state.viewRun && state.viewRun.id === next.id) { renderRun(next); }
            if (next && !isTerminal(next.status)) {
                state.pollDelay = POLL_MIN;
                schedulePoll(1200);
            } else {
                stopPoll();
                loadState({ quiet: true });
            }
            setNotice('已请求取消。已发出的请求仍可能产生费用。', 'info', false);
        }).catch(function (err) {
            if (dom.runCancel) { dom.runCancel.disabled = false; }
            setNotice(errorText(err), 'err', false);
        });
    }

    function openHistoryRun(index) {
        var runs = Array.isArray(state.history) ? state.history : [];
        var run = runs[index];
        if (!run || !run.id) { return; }
        if (!run.status_url) {
            renderRun(run);
            return;
        }
        requestJson(run.status_url).then(function (data) {
            if (data && data.quota) { state.quota = data.quota; renderQuota(); }
            var detailed = (data && data.run) ? data.run : run;
            renderRun(detailed);
        }).catch(function (err) {
            setNotice(errorText(err), 'err', false);
        });
    }

    /* ------------------------------------------------------------------ 绑定事件 */

    function bind() {
        document.addEventListener('click', function (event) {
            var target = event.target;
            if (!target || typeof target.closest !== 'function') { return; }

            if (target.closest('#fp-info-btn')) {
                event.preventDefault();
                toggleInfoHint();
                return;
            }
            if (!target.closest('#fp-hint')) { closeInfoHint(); }

            var retry = target.closest('#fp-notice-retry');
            if (retry) {
                event.preventDefault();
                retryPendingStart();
                return;
            }
            if (target.closest('#fp-run-cancel')) {
                event.preventDefault();
                cancelRun();
                return;
            }
            if (target.closest('#fp-run-again')) {
                event.preventDefault();
                var againModel = state.viewRun ? state.viewRun.model_id : '';
                if (againModel) {
                    setNotice('再次检测会创建新的收费任务。', 'info', false);
                    startDetection(againModel);
                }
                return;
            }
            if (target.closest('#fp-run-back')) {
                event.preventDefault();
                if (state.activeRun) { renderRun(state.activeRun); }
                return;
            }

            var historyItem = target.closest('[data-fp-history]');
            if (historyItem) {
                event.preventDefault();
                openHistoryRun(parseInt(historyItem.getAttribute('data-fp-history'), 10));
                return;
            }

            var detectButton = target.closest('[data-fp-start]');
            if (detectButton) {
                event.preventDefault();
                if (detectButton.disabled) { return; }
                startDetection(detectButton.getAttribute('data-fp-start') || '');
                return;
            }

            var chip = target.closest('.model-chip[data-fp-model]');
            if (chip) {
                event.preventDefault();
                toggleChip(chip);
                return;
            }

            if (target.closest('[data-fp-action]')) { return; }
            closeAllChips();
        });

        document.addEventListener('keydown', function (event) {
            var key = event.key;
            if (key === 'Escape' || key === 'Esc') {
                closeInfoHint();
                closeAllChips();
                return;
            }
            if (key !== 'Enter' && key !== ' ' && key !== 'Spacebar') { return; }
            var target = event.target;
            if (!target || typeof target.closest !== 'function') { return; }
            var chip = target.closest('.model-chip[data-fp-model]');
            if (!chip) { return; }
            // 模型条目用 span 呈现，这里补上 Enter/Space 的等价点击。
            event.preventDefault();
            toggleChip(chip);
        });

        if (dom.keySelect) {
            dom.keySelect.addEventListener('change', function () {
                state.selectedKeyId = dom.keySelect.value || '';
                applyGateToActions();
            });
        }

        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') {
                if (state.activeRun && !isTerminal(state.activeRun.status)) {
                    state.pollDelay = POLL_MIN;
                    schedulePoll(0);
                }
            }
        });

        var container = byId('model-catalog-body');
        if (container && window.MutationObserver) {
            var pending = false;
            var observer = new MutationObserver(function () {
                if (pending) { return; }
                pending = true;
                var run = function () { pending = false; upgradeChips(); };
                if (window.requestAnimationFrame) { window.requestAnimationFrame(run); }
                else { window.setTimeout(run, 0); }
            });
            observer.observe(container, { childList: true, subtree: true });
        }
    }

    function cacheDom() {
        dom.quotaText = byId('fp-quota-text');
        dom.quotaNext = byId('fp-quota-next');
        dom.keySelect = byId('fp-key-select');
        dom.keyNote = byId('fp-key-note');
        dom.card = byId('fingerprint-card');
        dom.notice = byId('fp-notice');
        dom.noticeText = byId('fp-notice-text');
        dom.noticeRetry = byId('fp-notice-retry');
        dom.infoBtn = byId('fp-info-btn');
        dom.infoHint = byId('fp-hint');
        dom.result = byId('fp-result');
        dom.runStatus = byId('fp-run-status');
        dom.runModel = byId('fp-run-model');
        dom.runTime = byId('fp-run-time');
        dom.runCancel = byId('fp-run-cancel');
        dom.runAgain = byId('fp-run-again');
        dom.runBack = byId('fp-run-back');
        dom.progressText = byId('fp-progress-text');
        dom.progressFill = byId('fp-progress-fill');
        dom.runError = byId('fp-run-error');
        dom.verdict = byId('fp-verdict');
        dom.runFamily = byId('fp-run-family');
        dom.runVerdict = byId('fp-run-verdict');
        dom.candidates = byId('fp-candidates');
        dom.candidatesBody = byId('fp-candidates-body');
        dom.candidatesEmpty = byId('fp-candidates-empty');
        dom.samplesWrap = byId('fp-samples-wrap');
        dom.samples = byId('fp-samples');
        dom.kvProtocol = byId('fp-kv-protocol');
        dom.kvKey = byId('fp-kv-key');
        dom.kvUpstream = byId('fp-kv-upstream');
        dom.kvBank = byId('fp-kv-bank');
        dom.paramsRow = byId('fp-row-params');
        dom.kvParams = byId('fp-kv-params');
        dom.durationRow = byId('fp-row-duration');
        dom.kvDuration = byId('fp-kv-duration');
        dom.usage = byId('fp-usage');
        dom.history = byId('fp-history');
        dom.historyList = byId('fp-history-list');
        dom.historyCount = byId('fp-history-count');
    }

    function init() {
        cacheDom();
        bind();
        upgradeChips();
        renderQuota();
        loadState();
    }

    init();
})();

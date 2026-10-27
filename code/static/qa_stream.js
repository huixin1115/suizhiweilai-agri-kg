/*
 * 流式问答（SSE）前端助手 —— 知识问答页与四类处方页共用
 * ==========================================================
 * 后端会先发 meta（检索结果），再一段段发 delta（大模型正文），最后发 done。
 * 这里负责：发请求 → 按帧解析 → 节流回调 → 出错回退。
 *
 * 用法：
 *   QAStream.run({
 *     url: '/api/qa/stream',
 *     body: fd,                       // FormData 或字符串
 *     headers: {...},                 // 可选
 *     onMeta:  m => {},               // 检索一结束就回调（类型 / 实体 / 溯源 / 追问）
 *     onDelta: txt => {},             // 累积到的正文（已节流，约 90ms 一次）
 *     onDone:  d => {},               // 完整结果，d.answer 才是最终答案
 *     onError: msg => {},
 *     fallback: () => {}              // 服务端没给 SSE（或流式失败且一个字都没收到）时走老接口
 *   });
 *
 * ⚠ 为什么不用 EventSource：它只支持 GET，而这里要 POST 一整个问题（可能很长）。
 * ⚠ onDelta 收到的是**累积全文**而不是增量：Markdown 要整篇重渲染，
 *   半个表格 / 单个 ** 都无法正确渲染，只能拿到全文再渲染。
 */
(function () {
    'use strict';

    function readError(res) {
        return res.json().catch(function () { return {}; })
            .then(function (d) { return d.error || d.message || ('HTTP ' + res.status); });
    }

    window.QAStream = {
        run: function (opt) {
            opt = opt || {};
            var acc = '', hid = null, gotDelta = false, done = false;
            var lastPaint = 0, timer = null;

            function paint(force) {
                if (typeof opt.onDelta !== 'function') return;
                var now = Date.now();
                if (!force && now - lastPaint < 90) {
                    if (timer) return;
                    timer = setTimeout(function () { timer = null; paint(true); }, 90);
                    return;
                }
                if (timer) { clearTimeout(timer); timer = null; }
                lastPaint = now;
                opt.onDelta(acc);
            }

            function handle(ev) {
                var t = ev && ev.event;
                if (t === 'meta') {
                    if (typeof opt.onMeta === 'function') opt.onMeta(ev);
                } else if (t === 'delta') {
                    acc += ev.text || '';
                    gotDelta = true;
                    paint(false);
                } else if (t === 'history') {
                    hid = ev.history_id || hid;
                } else if (t === 'done') {
                    done = true;
                    paint(true);
                    ev.history_id = ev.history_id || hid;
                    if (typeof opt.onDone === 'function') opt.onDone(ev);
                } else if (t === 'error') {
                    // 一个字都没收到就失败了 —— 退回到一次性接口，别让用户白等
                    if (!gotDelta && typeof opt.fallback === 'function') {
                        opt.fallback();
                        return;
                    }
                    if (typeof opt.onError === 'function') {
                        opt.onError(ev.message || '生成失败');
                    }
                }
            }

            var req = { method: 'POST', body: opt.body };
            if (opt.headers) req.headers = opt.headers;

            return fetch(opt.url, req).then(function (res) {
                var ct = (res.headers.get('content-type') || '').toLowerCase();
                if (!res.ok) return readError(res).then(function (m) { throw new Error(m); });
                // 服务端没走 SSE（老版本 / 被中间层改过响应头）：交给调用方的老逻辑
                if (ct.indexOf('text/event-stream') < 0 || !res.body) {
                    if (typeof opt.fallback === 'function') return opt.fallback(res);
                    throw new Error('服务端未返回流式响应');
                }

                var reader = res.body.getReader();
                var dec = new TextDecoder('utf-8');
                var buf = '';

                function pump() {
                    return reader.read().then(function (r) {
                        if (r.done) {
                            if (!done && typeof opt.onError === 'function') {
                                opt.onError('连接中断，没收到完整答案');
                            }
                            return;
                        }
                        buf += dec.decode(r.value, { stream: true });
                        var parts = buf.split('\n\n');
                        buf = parts.pop();                     // 最后一段可能还没收全
                        parts.forEach(function (frame) {
                            var line = null;
                            frame.split('\n').forEach(function (l) {
                                if (l.indexOf('data:') === 0 && line === null) line = l;
                            });
                            if (line === null) return;         // ': ping' 心跳注释，忽略
                            var obj;
                            try { obj = JSON.parse(line.slice(5).trim()); } catch (e) { return; }
                            handle(obj);
                        });
                        return pump();
                    });
                }
                return pump();
            }).catch(function (e) {
                if (done) return;
                if (typeof opt.fallback === 'function' && !gotDelta) {
                    opt.fallback();
                    return;
                }
                if (typeof opt.onError === 'function') {
                    opt.onError(e && e.message ? e.message : String(e));
                }
            });
        }
    };
})();

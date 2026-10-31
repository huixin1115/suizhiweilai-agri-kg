/* ============================================================
   跨天自动刷新（今日待办 / 物候期提醒 共用）
   ------------------------------------------------------------
   问题：页面一直开着，过了零点数据还停在"昨天"—— 待办的分组、
   地块宫格里的生育期进度和倒计时都是按"今天"算的，不重拉就不会更新。

   用法：
       DayRollover.watch(function(){ loadTodos(); });

   · 每分钟比对一次日期（页面最小化也照样跑）
   · 页面重新可见 / 重新聚焦 / 从前进后退缓存恢复时立刻比对一次
     （笔记本合盖第二天打开，这种方式最常见）
   · **只有日期真的变了才回调**，不会有一分钟一次的无效请求
   · 日期回退（系统时间被改）同样算"变了"，也会刷新
   ============================================================ */
(function () {
  'use strict';

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  /**
   * 本地日期 YYYY-MM-DD —— 与后端 datetime.date.today() 同一个口径。
   * ⚠ 别用 new Date().toISOString().slice(0,10)：那是 UTC，
   *   东八区在早上 8 点前会算成前一天。
   */
  function todayStr(d) {
    d = d || new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  /** 顶部轻提示：3 秒后自己消失（跨天刷新是无声发生的，得让人知道看的是新数据） */
  function toast(text) {
    try {
      var el = document.createElement('div');
      el.textContent = text;
      el.style.cssText = 'position:fixed;top:16px;left:50%;transform:translateX(-50%);'
        + 'z-index:9999;padding:10px 18px;border-radius:10px;font-size:14px;'
        + 'background:rgba(31,41,55,.94);color:#fff;box-shadow:0 6px 24px rgba(0,0,0,.18);'
        + 'transition:opacity .5s;opacity:1';
      document.body.appendChild(el);
      setTimeout(function () { el.style.opacity = '0'; }, 3000);
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 3600);
    } catch (e) { /* 提示失败不影响刷新本身 */ }
  }

  /**
   * 注册一个"日期变了就执行"的回调
   * @param {Function} fn   回调，收到 {from, to, reason}
   * @param {Object}  opts  {every: 60（秒）, toast: true（是否弹提示）, text: '提示文案'}
   */
  function watch(fn, opts) {
    opts = opts || {};
    var every = Math.max(10, opts.every || 60) * 1000;
    var last = todayStr();

    function check(reason) {
      var now = todayStr();
      if (now === last) return false;      // 日期没变，一次请求都不发
      var from = last;
      last = now;
      try {
        fn({ from: from, to: now, reason: reason || 'timer' });
        if (opts.toast !== false) toast(opts.text || '已跨到新的一天，页面数据已刷新');
      } catch (e) {
        if (window.console) console.warn('[DayRollover] 刷新回调出错：', e);
      }
      return true;
    }

    // 1) 定时兜底：页面一直开着（哪怕在后台标签页）也能跨过去
    setInterval(function () { check('timer'); }, every);

    // 2) 重新可见 / 聚焦 / bfcache 恢复：合盖再打开主要靠这个
    function onShow() { check('visible'); }
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') onShow();
    });
    window.addEventListener('focus', onShow);
    window.addEventListener('pageshow', onShow);

    return {
      /** 手动触发一次比对（调试用） */
      check: check,
      /** 当前认定的日期 */
      today: function () { return last; }
    };
  }

  window.DayRollover = { watch: watch, todayStr: todayStr, toast: toast };
})();

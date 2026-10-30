/* 欧朋AI 后台公共脚本：fetch 封装、Toast、弹窗、格式化 */
(function () {
  'use strict';

  window.$ = function (sel, root) { return (root || document).querySelector(sel); };
  window.$$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  window.escapeHtml = function (value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };
  // 页面里统一使用简写 esc()，这里做别名，避免各模板重复定义
  window.esc = window.escapeHtml;

  window.fmtTime = function (ts) {
    if (!ts) return '-';
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '-';
    var p = function (n) { return n < 10 ? '0' + n : '' + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  };

  window.fmtNum = function (n) {
    n = Number(n || 0);
    if (n >= 100000000) return (n / 100000000).toFixed(1) + ' 亿';
    if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
    return String(n);
  };

  window.api = async function (path, options) {
    var opts = Object.assign({}, options || {});
    opts.headers = Object.assign({}, opts.headers || {});
    if (opts.body && typeof opts.body !== 'string' && !(opts.body instanceof FormData)) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    var res = await fetch(path, opts);
    var data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401 && path.indexOf('/login') < 0) {
      location.href = '/db_admin/login';
      throw new Error('登录已过期');
    }
    if (!res.ok) {
      var err = data && data.error;
      var msg = typeof err === 'string' ? err
        : (err && (err.message || err.code)) || (data && data.error) || ('HTTP ' + res.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  };

  window.toast = function (msg, type) {
    var box = document.getElementById('toast-box');
    if (!box) {
      box = document.createElement('div');
      box.id = 'toast-box';
      document.body.appendChild(box);
    }
    var el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () {
      el.classList.add('out');
      setTimeout(function () { el.remove(); }, 300);
    }, 3200);
  };

  window.openModal = function (title, bodyHtml, onSave, opts) {
    opts = opts || {};
    var mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML =
      '<div class="modal ' + (opts.wide ? 'wide' : '') + '">' +
      '<div class="modal-head"><h3>' + window.escapeHtml(title) + '</h3>' +
      '<button class="icon-btn" data-close type="button">&times;</button></div>' +
      '<div class="modal-body">' + bodyHtml + '</div>' +
      '<div class="modal-foot"><button class="btn" data-close type="button">取消</button>' +
      (onSave ? '<button class="btn btn-primary" data-save type="button">保存</button>' : '') +
      '</div></div>';
    document.body.appendChild(mask);
    mask.addEventListener('click', function (e) {
      // 只有明确带 data-close 的按钮（右上角 × / 取消）才关闭；
      // 点击遮罩阴影不做任何动作，避免误触丢失正在编辑的内容
      if (e.target.hasAttribute('data-close')) mask.remove();
    });
    if (onSave) {
      var btn = mask.querySelector('[data-save]');
      btn.addEventListener('click', async function () {
        btn.disabled = true;
        var old = btn.textContent;
        btn.textContent = '保存中…';
        try {
          await onSave(mask);
          mask.remove();
        } catch (err) {
          window.toast(err.message || '保存失败', 'error');
          btn.disabled = false;
          btn.textContent = old;
        }
      });
    }
    return mask;
  };

  window.logout = async function () {
    try { await window.api('/db_admin/api/logout', { method: 'POST' }); } catch (e) {}
    location.href = '/db_admin/login';
  };

  window.copyText = async function (text, tip) {
    try {
      await navigator.clipboard.writeText(text);
      window.toast(tip || '已复制到剪贴板', 'ok');
    } catch (e) {
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      window.toast(tip || '已复制到剪贴板', 'ok');
    }
  };

  // 类型徽标
  var TYPE_LABELS = { text: '文本', vision: '图片理解', ocr: '图片识别',
    gen: '生图', video_gen: '生视频', video_vision: '视频识别' };
  window.typeLabel = function (t) { return TYPE_LABELS[t] || t; };
  window.typeBadges = function (types) {
    return (types || []).map(function (t) {
      return '<span class="badge primary">' + window.escapeHtml(TYPE_LABELS[t] || t) + '</span>';
    }).join(' ');
  };
})();
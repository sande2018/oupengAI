# -*- coding: utf-8 -*-
"""多渠道路由调度核心（参考 ai.py 的「渠道 → 模型 → Key」组合思路）。

一次业务调用会展开成若干候选组合，按优先级/权重排序后逐个尝试：
  1. 同一个渠道里有多组 Key → 轮换起点，失效自动切下一个 Key；
  2. 一个模型挂了 → 切下一个渠道的模型；
  3. Key 级冷却（401/403/429）与渠道级熔断（连续 5 次失败冷却 60s）；
  4. 上游流式请求在建立阶段失败同样会自动切换，保证首包前完成故障转移。
"""
import json
import threading
import time

import requests

from config import (
    AI_TYPES, AI_TYPE_ORDER, CHAT_TYPES, IMAGE_TYPE_CHAIN,
    CONNECT_TIMEOUT, DEFAULT_UPSTREAM_TIMEOUT, RETRY_PER_CANDIDATE,
    CLIENT_ERROR_CODES, KEY_COOLDOWN_CODES, KEY_COOLDOWN_SECONDS,
    KEY_RATE_LIMIT_COOLDOWN, CHANNEL_FAIL_THRESHOLD, CHANNEL_COOLDOWN_SECONDS,
)
from db import query_all, query_one, execute, ts

# ---------- 进程内状态 ----------
_lock = threading.Lock()
_key_rr = {}      # channel_model_id -> Key 轮换计数
_swrr_state = {}  # (custom_model_id, mtype) -> {target_id: current_weight}
_tls = threading.local()


def _http():
    """按线程复用 requests.Session（连接池）。"""
    s = getattr(_tls, 'session', None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50)
        s.mount('http://', adapter)
        s.mount('https://', adapter)
        _tls.session = s
    return s


class RouteError(Exception):
    """路由失败：status 为应返回给客户端的状态码，attempts 记录每一次尝试。"""

    def __init__(self, status, message, attempts=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.attempts = attempts or []

    def to_openai(self):
        return {'error': {'message': self.message,
                          'type': 'upstream_error' if self.status >= 500 else 'invalid_request_error',
                          'code': self.status,
                          'attempts': self.attempts}}


# ---------- JSON 字段容错 ----------
def parse_types(raw):
    """解析模型的能力类型 JSON（容错），只保留合法类型并按固定顺序输出。"""
    try:
        arr = json.loads(raw or '[]')
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    picked = []
    for t in arr:
        t = str(t or '').strip()
        if t in AI_TYPES and t not in picked:
            picked.append(t)
    return [t for t in AI_TYPE_ORDER if t in picked]


def type_chain(mtype):
    """某类请求的兜底类型链：图片理解失败可退 OCR 模型，OCR 失败可退视觉模型，
    纯文本请求可退视觉模型（视觉模型也能读文本），视频识别/生图/生视频不兜底。"""
    if mtype == 'vision':
        return ['vision', 'ocr']
    if mtype == 'ocr':
        return ['ocr', 'vision']
    if mtype == 'text':
        return ['text', 'vision']
    return [mtype]


# ---------- 能力探测 ----------
def available_types():
    """返回当前配置下每类能力可用与否（后台/介绍页展示用）。"""
    out = {t: False for t in AI_TYPE_ORDER}
    rows = query_all('SELECT m.types, m.enabled, m.model_name FROM channel_models m '
                     'JOIN channels c ON c.id = m.channel_id WHERE c.enabled = 1')
    for r in rows:
        if not r['enabled']:
            continue
        for t in parse_types(r['types']):
            out[t] = True
    return out


def has_type(mtype):
    return available_types().get(mtype, False)


# ---------- 候选构建 ----------
_SQL_TARGETS = """
SELECT m.id            AS model_id,
       m.channel_id    AS channel_id,
       m.model_name    AS model_name,
       m.types         AS types,
       m.timeout       AS model_timeout,
       m.priority      AS model_priority,
       m.weight        AS model_weight,
       m.sort          AS model_sort,
       m.disabled_until AS model_disabled_until,
       t.id            AS target_id,
       t.priority      AS target_priority,
       t.weight        AS target_weight,
       t.sort          AS target_sort,
       c.name          AS channel_name,
       c.base_url      AS base_url,
       c.timeout       AS channel_timeout,
       c.video_path    AS video_path,
       c.disabled_until AS channel_disabled_until
  FROM custom_model_targets t
  JOIN channel_models m ON m.id = t.channel_model_id
  JOIN channels c       ON c.id = m.channel_id
 WHERE t.custom_model_id = ? AND t.enabled = 1
   AND m.enabled = 1 AND c.enabled = 1
"""

_SQL_ALL = """
SELECT m.id            AS model_id,
       m.channel_id    AS channel_id,
       m.model_name    AS model_name,
       m.types         AS types,
       m.timeout       AS model_timeout,
       m.priority      AS model_priority,
       m.weight        AS model_weight,
       m.sort          AS model_sort,
       m.disabled_until AS model_disabled_until,
       0               AS target_id,
       0               AS target_priority,
       m.weight        AS target_weight,
       m.sort          AS target_sort,
       c.name          AS channel_name,
       c.base_url      AS base_url,
       c.timeout       AS channel_timeout,
       c.video_path    AS video_path,
       c.disabled_until AS channel_disabled_until
  FROM channel_models m
  JOIN channels c ON c.id = m.channel_id
 WHERE m.enabled = 1 AND c.enabled = 1
"""

_SQL_BY_MODEL = _SQL_ALL + ' AND m.model_name = ?'


def _weighted_order(groups, state_key):
    """平滑加权轮询（SWRR）：groups = [(priority, [rows...]), ...]。

    state 按 (自定义模型, 类型) 跨请求保留，保证连续请求不会总是命中同一个模型，
    失败切换时则会顺延到当前顺序里的下一个候选。priority 小的分组先于大分组。
    """
    with _lock:
        state = _swrr_state.setdefault(state_key, {})
        alive = {it['target_id'] for _, group in groups for it in group}
        for tid in list(state.keys()):
            if tid not in alive:
                state.pop(tid, None)
        result = []
        for _, group in groups:
            if len(group) <= 1:
                result.extend(group)
                continue
            for it in group:
                state.setdefault(it['target_id'], 0)
            remaining = list(group)
            for _ in range(len(group)):
                total = sum(it['target_weight'] for it in remaining) or 1
                best = None
                for it in remaining:
                    state[it['target_id']] += it['target_weight']
                    if best is None or state[it['target_id']] > state[best['target_id']]:
                        best = it
                state[best['target_id']] -= total
                result.append(best)
                remaining.remove(best)
        return result


def _order_rows(rows, strategy, state_key):
    """按策略排序：priority（权重只作次要因素）/ weighted（组内加权轮询）。"""
    if not rows:
        return []
    if strategy == 'weighted':
        groups = {}
        for r in rows:
            groups.setdefault(r['target_priority'], []).append(r)
        ordered = []
        pairs = []
        for pri in sorted(groups.keys()):
            group = sorted(groups[pri], key=lambda x: (x['target_sort'], x['model_sort'],
                                                       x['model_id']))
            pairs.append((pri, group))
        ordered = _weighted_order(pairs, state_key)
    else:
        ordered = sorted(rows, key=lambda x: (x['target_priority'], x['target_sort'],
                                              x['model_priority'], x['model_sort'],
                                              x['model_id']))
    # 渠道处于熔断冷却期的排到最后（半开，仍会尝试）
    now = ts()
    return sorted(ordered, key=lambda x: 1 if (x['channel_disabled_until'] or 0) > now else 0)


def _keys_for_channel(channel_id, now):
    """取渠道可用的 Key 并轮换起点。全部冷却时取最早解冻的一个做半开尝试。"""
    rows = query_all('SELECT * FROM channel_keys WHERE channel_id=? AND enabled=1 '
                     'ORDER BY sort, id', (channel_id,))
    if not rows:
        # 渠道没配 Key：允许空 Key 调用（兼容免鉴权中转）
        return [{'id': 0, 'api_key': '', 'label': 'nokey', 'cooling': False}]
    keys = [dict(k) for k in rows]
    alive = [k for k in keys if (k.get('disabled_until') or 0) <= now]
    if not alive:
        keys.sort(key=lambda k: k.get('disabled_until') or 0)
        alive = keys[:1]
        alive[0]['cooling'] = True
    n = len(alive)
    with _lock:
        start = _key_rr.get(channel_id, 0) % n
        _key_rr[channel_id] = start + 1
    ordered = alive[start:] + alive[:start]
    out = []
    for idx, k in enumerate(ordered):
        label = (k.get('label') or '').strip()
        if not label:
            label = ('key#%d' % (idx + 1)) if len(keys) > 1 else 'default'
        if not k['api_key']:
            label = 'nokey'
        out.append({'id': k['id'], 'api_key': k['api_key'], 'label': label,
                    'cooling': bool(k.get('cooling'))})
    return out


def build_candidates(mtype, model_name=None):
    """展开候选组合。

    - model_name 命中自定义模型：只用它勾选的渠道模型；
    - 自定义模型没勾任何目标：回退到全部启用的通道（开箱即用）；
    - model_name 不是自定义模型：视作上游真实模型名，只在同名模型里找；
    - 传 None：全部启用通道。
    """
    if mtype not in AI_TYPES:
        return []
    custom = query_one('SELECT * FROM custom_models WHERE name=?', (model_name,)) if model_name else None
    if custom and not custom['enabled']:
        return []

    if custom:
        rows = [dict(r) for r in query_all(_SQL_TARGETS, (custom['id'],))]
        if not rows:
            rows = [dict(r) for r in query_all(_SQL_ALL)]
        strategy = custom['strategy']
        state_key = (custom['id'], mtype)
    else:
        if model_name:
            rows = [dict(r) for r in query_all(_SQL_BY_MODEL, (model_name,))]
        else:
            rows = [dict(r) for r in query_all(_SQL_ALL)]
        strategy = 'priority'
        state_key = ('*', mtype)

    rows = [r for r in rows if mtype in parse_types(r['types'])]
    if not rows:
        return []

    ordered = _order_rows(rows, strategy, state_key)
    now = ts()
    combos = []
    for r in ordered:
        timeout = int(r['model_timeout'] or 0) or int(r['channel_timeout'] or 0) or DEFAULT_UPSTREAM_TIMEOUT
        for k in _keys_for_channel(r['channel_id'], now):
            combos.append({
                'channel_id': r['channel_id'],
                'channel_name': r['channel_name'],
                'base_url': r['base_url'],
                'video_path': r['video_path'],
                'model_id': r['model_id'],
                'model': r['model_name'],
                'type': mtype,
                'target_id': r['target_id'],
                'priority': r['target_priority'],
                'weight': r['target_weight'],
                'timeout': timeout,
                'key_id': k['id'],
                'key': k['api_key'],
                'key_label': k['label'],
                'key_cooling': k['cooling'],
            })
    return combos


def build_chain_candidates(mtype, model_name=None):
    """按兜底链合并候选（同一渠道模型 + 同一 Key 只出现一次）。"""
    out, seen = [], set()
    for t in type_chain(mtype):
        for c in build_candidates(t, model_name):
            mark = (c['model_id'], c['key_id'])
            if mark in seen:
                continue
            seen.add(mark)
            out.append(c)
    return out


# ---------- 健康度反馈 ----------
def mark_key_success(key_id):
    if not key_id:
        return
    execute('UPDATE channel_keys SET success_count=success_count+1, fail_count=0, '
            'disabled_until=0, last_used_at=? WHERE id=?', (ts(), key_id))


def mark_key_failure(key, status_code, retry_after=None):
    """按状态码给 Key 打冷却标记：401/403 → 5 分钟，429 → 退避，其它失败 3 次 → 2 分钟。

    key 既可以是 channel_keys 行（含 id），也可以是候选组合（含 key_id）。
    """
    key_id = (key.get('id') or key.get('key_id')) if key else 0
    if not key_id:
        return
    now = ts()
    if status_code in KEY_COOLDOWN_CODES:
        wait = KEY_RATE_LIMIT_COOLDOWN if status_code == 429 else KEY_COOLDOWN_SECONDS
        if retry_after:
            try:
                wait = max(wait, int(float(retry_after)))
            except (TypeError, ValueError):
                pass
        execute('UPDATE channel_keys SET fail_count=fail_count+1, disabled_until=?, '
                'last_used_at=? WHERE id=?', (now + wait, now, key_id))
        return
    row = query_one('SELECT fail_count FROM channel_keys WHERE id=?', (key_id,))
    fails = (row['fail_count'] if row else 0) + 1
    disabled = now + 120 if fails >= 3 else 0
    execute('UPDATE channel_keys SET fail_count=?, disabled_until=?, last_used_at=? WHERE id=?',
            (0 if disabled else fails, disabled, now, key_id))


def mark_channel_failure(channel_id):
    row = query_one('SELECT fail_count FROM channels WHERE id=?', (channel_id,))
    fails = (row['fail_count'] if row else 0) + 1
    if fails >= CHANNEL_FAIL_THRESHOLD:
        execute('UPDATE channels SET fail_count=0, disabled_until=?, updated_at=? WHERE id=?',
                (ts() + CHANNEL_COOLDOWN_SECONDS, ts(), channel_id))
    else:
        execute('UPDATE channels SET fail_count=?, updated_at=? WHERE id=?',
                (fails, ts(), channel_id))


def mark_channel_success(channel_id):
    execute('UPDATE channels SET fail_count=0, disabled_until=0, last_error="" WHERE id=?',
            (channel_id,))


def mark_model_failure(model_id):
    execute('UPDATE channel_models SET fail_count=fail_count+1, updated_at=? WHERE id=?',
            (ts(), model_id))


# ---------- 上游请求 ----------
def _join_url(base_url, path):
    return (base_url or '').rstrip('/') + path


def _retry_after(resp):
    try:
        return resp.headers.get('Retry-After')
    except Exception:
        return None


def _execute_combos(combos, endpoint, payload, timeout=None, stream=False,
                    extra_headers=None, track=True):
    """按给定候选顺序执行上游请求（request_upstream / request_single / request_by_name 共用）。

    track=False 用于后台连通性测试：只发请求，不写 Key/渠道健康状态，
    避免一次测试失败影响正常调度。
    """
    attempts = []
    t0 = time.time()
    for cand in combos:
        body = dict(payload)
        body['model'] = cand['model']
        headers = {'Content-Type': 'application/json'}
        if cand['key']:
            headers['Authorization'] = 'Bearer %s' % cand['key']
        if extra_headers:
            headers.update(extra_headers)
        url = _join_url(cand['base_url'], endpoint or cand['video_path'] or '/videos/generations')
        per_timeout = timeout or cand['timeout']
        req_timeout = (CONNECT_TIMEOUT, per_timeout)
        tries = 1 + max(0, RETRY_PER_CANDIDATE)
        for attempt in range(tries):
            try:
                resp = _http().post(url, headers=headers,
                                    data=json.dumps(body, ensure_ascii=False),
                                    timeout=req_timeout, stream=stream)
            except requests.exceptions.RequestException as e:
                msg = '%s' % e
                attempts.append({'channel': cand['channel_name'], 'model': cand['model'],
                                 'key': cand['key_label'], 'status': 0, 'error': msg[:300]})
                if track:
                    mark_key_failure(cand, 0)
                    mark_channel_failure(cand['channel_id'])
                resp = None
                if attempt + 1 < tries:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                break
            if resp.status_code == 200:
                if track:
                    mark_key_success(cand['key_id'])
                    mark_channel_success(cand['channel_id'])
                return resp, cand, int((time.time() - t0) * 1000), attempts

            snippet = ''
            try:
                snippet = (resp.text or '')[:300]
            except Exception:
                pass
            attempts.append({'channel': cand['channel_name'], 'model': cand['model'],
                             'key': cand['key_label'], 'status': resp.status_code,
                             'error': snippet})
            if track:
                execute('UPDATE channels SET last_error=?, updated_at=? WHERE id=?',
                        ('HTTP %s %s' % (resp.status_code, snippet[:150]),
                         ts(), cand['channel_id']))
                mark_key_failure(cand, resp.status_code, _retry_after(resp))
                if resp.status_code in CLIENT_ERROR_CODES:
                    mark_model_failure(cand['model_id'])
                elif resp.status_code >= 500:
                    mark_channel_failure(cand['channel_id'])
            # 其余 4xx（402 余额不足等）视为该组合不可用，直接换下一个
            try:
                resp.close()
            except Exception:
                pass
            break

    last = attempts[-1] if attempts else {}
    detail = '；'.join('%s/%s[%s] → %s %s' % (
        a['channel'], a['model'], a['key'], a['status'] or 'ERR', a['error'][:120])
        for a in attempts[-3:])
    raise RouteError(502, '所有上游模型均调用失败：%s' % (detail or '未知错误'), attempts)


def request_upstream(mtype, endpoint, payload, model_name=None, timeout=None,
                     stream=False, extra_headers=None, max_attempts=12):
    """按候选顺序调用上游，返回 (response, candidate, elapsed_ms, attempts)。

    endpoint 传 None 时使用渠道上配置的 video_path（生视频端点各家不统一）。
    成功（HTTP 200）即返回；全部失败抛出 RouteError(502/503)。
    """
    combos = build_chain_candidates(mtype, model_name)[:max_attempts]
    if not combos:
        label = AI_TYPES.get(mtype, {}).get('label', mtype)
        raise RouteError(503, '没有可用的「%s」模型：请到后台渠道里勾选该类型的模型，'
                              '或把它加入自定义模型的调度清单' % label)
    return _execute_combos(combos, endpoint, payload, timeout, stream, extra_headers)


def request_single(model_id, mtype, endpoint, payload, key_id=None, timeout=None,
                   stream=False, extra_headers=None):
    """后台连通性测试：只打指定的渠道模型（可指定 Key），不走自定义模型调度。"""
    row = query_one(_SQL_ALL + ' AND m.id=?', (model_id,))
    if not row:
        raise RouteError(404, '模型不存在，或所在渠道已停用')
    row = dict(row)
    now = ts()
    combos = []
    for k in _keys_for_channel(row['channel_id'], now):
        if key_id and k['id'] != key_id:
            continue
        combos.append({
            'channel_id': row['channel_id'],
            'channel_name': row['channel_name'],
            'base_url': row['base_url'],
            'video_path': row['video_path'],
            'model_id': row['model_id'],
            'model': row['model_name'],
            'type': mtype,
            'target_id': 0,
            'priority': row['model_priority'],
            'weight': row['model_weight'],
            'timeout': int(row['model_timeout'] or 0) or int(row['channel_timeout'] or 0)
            or DEFAULT_UPSTREAM_TIMEOUT,
            'key_id': k['id'],
            'key': k['api_key'],
            'key_label': k['label'],
            'key_cooling': k['cooling'],
        })
    if not combos:
        raise RouteError(400, '该渠道没有可用的 API Key')
    return _execute_combos(combos[:3], endpoint, payload, timeout, stream,
                           extra_headers, track=False)


def request_by_name(channel_id, model_name, mtype, endpoint, payload, timeout=None,
                    stream=False, extra_headers=None):
    """后台连通性测试：按「渠道 + 上游模型名」直接发请求（模型可能还没导入）。

    只发前 3 个可用 Key，不写健康状态。
    """
    if not model_name:
        raise RouteError(400, '缺少上游模型名')
    channel = query_one('SELECT * FROM channels WHERE id=?', (channel_id,))
    if not channel:
        raise RouteError(404, '渠道不存在')
    channel = dict(channel)
    combos = []
    for k in _keys_for_channel(channel_id, ts()):
        combos.append({
            'channel_id': channel_id,
            'channel_name': channel['name'],
            'base_url': channel['base_url'],
            'video_path': channel['video_path'],
            'model_id': 0,
            'model': model_name,
            'type': mtype,
            'target_id': 0,
            'priority': 0,
            'weight': 100,
            'timeout': int(channel['timeout'] or 0) or DEFAULT_UPSTREAM_TIMEOUT,
            'key_id': k['id'],
            'key': k['api_key'],
            'key_label': k['label'],
            'key_cooling': k['cooling'],
        })
    if not combos:
        raise RouteError(400, '该渠道没有可用的 API Key')
    return _execute_combos(combos[:3], endpoint, payload, timeout, stream,
                           extra_headers, track=False)


# ---------- 业务侧类型判定（智能路由入口） ----------
_OCR_HINTS = ('识别文字', '提取文字', '文字识别', '图片转文字', '转成文字', '读取文字',
              'ocr', '扫描件', '票据识别', '发票识别', '菜单识别', '身份证识别',
              '营业执照', '小票', '识别图中文字', '识别图片中的文字')


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get('type') in ('text', 'input_text'):
                parts.append(str(p.get('text') or ''))
        return ' '.join(parts)
    return ''


def detect_chat_type(body, header_hint=None):
    """判定一次 chat 请求该调度哪类模型。

    优先级：请求头 X-Task-Type > 请求体 task_type > 内容判定。
    内容里带 video_url → video_vision；带 image_url → vision（提示词明显是 OCR 意图时 → ocr）；
    纯文本 → text。
    """
    for hint in (header_hint, (body or {}).get('task_type')):
        hint = str(hint or '').strip().lower()
        if hint in AI_TYPES:
            return hint

    messages = (body or {}).get('messages') or []
    has_video = has_image = False
    last_user_text = ''
    for m in messages:
        content = m.get('content') if isinstance(m, dict) else None
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                t = part.get('type')
                if t in ('image_url', 'input_image', 'image'):
                    has_image = True
                elif t in ('video_url', 'input_video', 'video'):
                    has_video = True
        if isinstance(m, dict) and m.get('role') == 'user':
            last_user_text = _text_of(content) or last_user_text

    if has_video:
        return 'video_vision'
    if has_image:
        lower = last_user_text.lower()
        if any(h in lower for h in _OCR_HINTS):
            return 'ocr'
        return 'vision'
    return 'text'


def image_part(data_url):
    """把 base64 / URL 统一成 OpenAI 多模态 image_url 片段。"""
    if not data_url:
        return None
    if data_url.startswith('data:'):
        url = data_url
    elif data_url.startswith('http://') or data_url.startswith('https://'):
        url = data_url
    else:
        url = 'data:image/jpeg;base64,' + data_url
    return {'type': 'image_url', 'image_url': {'url': url}}


TINY_PNG_B64 = (
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+pLw5AAAAAElFTkSuQmCC'
)


def tiny_image_url():
    return 'data:image/png;base64,' + TINY_PNG_B64
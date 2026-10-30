# -*- coding: utf-8 -*-
"""管理后台 JSON API（/db_admin/api/*）。

页面只负责渲染，所有数据读写都走这里，方便日后接其它前端或脚本。
"""
import json
import re
import secrets
import time

from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from config import (AI_TYPES, AI_TYPE_ORDER, DEFAULT_CHUNK_OVERLAP,
                    DEFAULT_CHUNK_SIZE, DEFAULT_MIN_SCORE, DEFAULT_TOP_K,
                    RAG_POLISH_BATCH_CHARS, RAG_POLISH_MAX_BATCHES,
                    RAG_POLISH_MAX_CHARS, VECTOR_DIM)
from db import (execute, get_setting, insert, query_all, query_one,
                set_setting, ts, update)
from services import knowledge, router, testcases

bp = Blueprint('admin_api', __name__, url_prefix='/db_admin/api')


# ---------- 基础工具 ----------
def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _bool(v, default=0):
    if v is None:
        return default
    return 1 if v in (1, True, '1', 'true', 'True', 'on', 'yes', '是') else 0


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _mask(key):
    key = key or ''
    if not key:
        return ''
    if len(key) <= 10:
        return '***'
    return key[:6] + '…' + key[-4:]


def _clean_types(value):
    arr = value if isinstance(value, list) else []
    picked = {str(x).strip() for x in arr}
    return [t for t in AI_TYPE_ORDER if t in picked]


def _ok(**kw):
    out = {'ok': True}
    out.update(kw)
    return jsonify(out)


# ---------- 登录态 ----------
@bp.before_request
def _require_login():
    if request.endpoint == 'admin_api.login':
        return None
    if session.get('admin'):
        return None
    return jsonify({'error': '未登录或登录已过期'}), 401


@bp.post('/login')
def login():
    data = _payload()
    password = str(data.get('password') or '')
    stored = get_setting('admin_password_hash')
    if not password or not stored or not check_password_hash(stored, password):
        return jsonify({'error': '密码错误'}), 401
    session.permanent = True
    session['admin'] = True
    return _ok(password_is_default=get_setting('admin_password_default') == '1')


@bp.post('/logout')
def logout():
    session.clear()
    return _ok()


@bp.get('/me')
def me():
    return jsonify({
        'ok': True,
        'password_is_default': get_setting('admin_password_default') == '1',
        'ai_types': [{'key': t, 'label': AI_TYPES[t]['label'], 'desc': AI_TYPES[t]['desc'],
                      'icon': AI_TYPES[t]['icon']} for t in AI_TYPE_ORDER],
    })


@bp.post('/password')
def change_password():
    data = _payload()
    old = str(data.get('old_password') or '')
    new = str(data.get('new_password') or '')
    stored = get_setting('admin_password_hash')
    if not stored or not check_password_hash(stored, old):
        return jsonify({'error': '原密码不正确'}), 400
    if len(new) < 6:
        return jsonify({'error': '新密码至少 6 位'}), 400
    set_setting('admin_password_hash', generate_password_hash(new))
    set_setting('admin_password_default', '0')
    return _ok()


# ---------- 概览 ----------
@bp.get('/overview')
def overview():
    def count(sql, params=()):
        row = query_one(sql, params)
        return int(row['n']) if row else 0

    since = time.time() - 86400
    stat = query_one('SELECT COUNT(*) AS n, COALESCE(SUM(ok),0) AS ok, '
                     'COALESCE(AVG(latency_ms),0) AS lat, '
                     'COALESCE(SUM(prompt_tokens),0) AS pt, '
                     'COALESCE(SUM(completion_tokens),0) AS ct '
                     'FROM call_logs WHERE created_at>=?', (since,))
    total = int(stat['n'] or 0) if stat else 0
    ok_n = int(stat['ok'] or 0) if stat else 0
    return jsonify({
        'ok': True,
        'counts': {
            'channels': count('SELECT COUNT(*) AS n FROM channels'),
            'channels_on': count('SELECT COUNT(*) AS n FROM channels WHERE enabled=1'),
            'models': count('SELECT COUNT(*) AS n FROM channel_models'),
            'models_on': count('SELECT COUNT(*) AS n FROM channel_models WHERE enabled=1'),
            'custom_models': count('SELECT COUNT(*) AS n FROM custom_models'),
            'kbs': count('SELECT COUNT(*) AS n FROM knowledge_bases'),
            'docs': count('SELECT COUNT(*) AS n FROM kb_documents'),
            'chunks': count('SELECT COUNT(*) AS n FROM kb_chunks'),
            'api_keys': count('SELECT COUNT(*) AS n FROM api_keys'),
            'api_keys_on': count('SELECT COUNT(*) AS n FROM api_keys WHERE enabled=1'),
        },
        'last24h': {
            'calls': total,
            'success': ok_n,
            'failed': max(0, total - ok_n),
            'success_rate': round(ok_n * 100.0 / total, 1) if total else 0,
            'avg_latency_ms': int(stat['lat'] or 0) if stat else 0,
            'prompt_tokens': int(stat['pt'] or 0) if stat else 0,
            'completion_tokens': int(stat['ct'] or 0) if stat else 0,
        },
        'types': [{'key': t, 'label': AI_TYPES[t]['label'], 'available': v}
                  for t, v in router.available_types().items()],
        'recent': [dict(r) for r in query_all(
            'SELECT id, endpoint, request_model, resolved_type, channel_name, '
            'upstream_model, ok, status_code, latency_ms, created_at '
            'FROM call_logs ORDER BY id DESC LIMIT 12')],
    })


# ---------- 渠道 ----------
def _save_keys(channel_id, keys):
    """同步渠道的 Key 列表：有 id 的更新（空 key 表示保留原值），没 id 的新增，缺的删除。"""
    keys = keys or []
    existing = {k['id']: dict(k) for k in
                query_all('SELECT * FROM channel_keys WHERE channel_id=?', (channel_id,))}
    keep = set()
    for idx, item in enumerate(keys):
        if not isinstance(item, dict):
            continue
        kid = _int(item.get('id'))
        api_key = str(item.get('api_key') or '').strip()
        label = str(item.get('label') or '').strip()[:60]
        enabled = _bool(item.get('enabled', 1))
        if kid and kid in existing:
            execute('UPDATE channel_keys SET label=?, enabled=?, sort=? WHERE id=?',
                    (label, enabled, idx, kid))
            if api_key and api_key != existing[kid]['api_key']:
                execute('UPDATE channel_keys SET api_key=?, disabled_until=0, fail_count=0 '
                        'WHERE id=?', (api_key, kid))
            keep.add(kid)
        elif api_key:
            dup = query_one('SELECT id FROM channel_keys WHERE channel_id=? AND api_key=?',
                            (channel_id, api_key))
            if dup:
                execute('UPDATE channel_keys SET label=?, enabled=?, sort=?, '
                        'disabled_until=0, fail_count=0 WHERE id=?',
                        (label, enabled, idx, dup['id']))
                keep.add(dup['id'])
            else:
                keep.add(insert('channel_keys', {
                    'channel_id': channel_id, 'api_key': api_key, 'label': label,
                    'enabled': enabled, 'sort': idx, 'created_at': ts(),
                }))
    for kid in existing:
        if kid not in keep:
            execute('DELETE FROM channel_keys WHERE id=?', (kid,))


def _channel_detail(row):
    d = dict(row)
    keys = query_all('SELECT * FROM channel_keys WHERE channel_id=? ORDER BY sort, id',
                     (row['id'],))
    d['keys'] = [{'id': k['id'], 'label': k['label'], 'masked': _mask(k['api_key']),
                  'enabled': k['enabled'], 'sort': k['sort'],
                  'disabled_until': k['disabled_until'],
                  'success_count': k['success_count'], 'fail_count': k['fail_count']}
                 for k in keys]
    d['key_count'] = len(keys)
    d['models'] = []
    for m in query_all('SELECT * FROM channel_models WHERE channel_id=? ORDER BY sort, id',
                       (row['id'],)):
        d['models'].append({'id': m['id'], 'model_name': m['model_name'],
                            'types': router.parse_types(m['types']),
                            'enabled': m['enabled'], 'priority': m['priority'],
                            'weight': m['weight'], 'timeout': m['timeout']})
    return d


@bp.get('/channels')
def list_channels():
    rows = query_all('SELECT * FROM channels ORDER BY sort, id')
    return jsonify({'ok': True, 'data': [_channel_detail(r) for r in rows]})


def _read_channel(data):
    name = str(data.get('name') or '').strip()
    base_url = str(data.get('base_url') or '').strip().rstrip('/')
    if not name:
        raise ValueError('渠道名称不能为空')
    if not (base_url.startswith('http://') or base_url.startswith('https://')):
        raise ValueError('Base URL 必须以 http:// 或 https:// 开头')
    video_path = str(data.get('video_path') or '/videos/generations').strip()
    if not video_path.startswith('/'):
        video_path = '/' + video_path
    return {
        'name': name,
        'base_url': base_url,
        'enabled': _bool(data.get('enabled'), 1),
        'sort': _int(data.get('sort')),
        'timeout': max(5, _int(data.get('timeout'), 180)),
        'video_path': video_path,
        'remark': str(data.get('remark') or '')[:500],
        'updated_at': ts(),
    }


@bp.post('/channels')
def create_channel():
    data = _payload()
    try:
        fields = _read_channel(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    fields['created_at'] = ts()
    cid = insert('channels', fields)
    _save_keys(cid, data.get('keys'))
    return _ok(id=cid), 201


@bp.put('/channels/<int:cid>')
def update_channel(cid):
    row = query_one('SELECT * FROM channels WHERE id=?', (cid,))
    if not row:
        return jsonify({'error': '渠道不存在'}), 404
    data = _payload()
    try:
        fields = _read_channel(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    update('channels', fields, 'id=?', (cid,))
    if 'keys' in data:
        _save_keys(cid, data.get('keys'))
    return _ok()


@bp.delete('/channels/<int:cid>')
def delete_channel(cid):
    execute('DELETE FROM channels WHERE id=?', (cid,))
    return _ok()


@bp.post('/channels/<int:cid>/toggle')
def toggle_channel(cid):
    """快速启用/停用渠道（停用后该渠道所有模型都不参与调度）。"""
    row = query_one('SELECT id, name, enabled FROM channels WHERE id=?', (cid,))
    if not row:
        return jsonify({'error': '渠道不存在'}), 404
    enabled = 0 if row['enabled'] else 1
    update('channels', {'enabled': enabled, 'updated_at': ts()}, 'id=?', (cid,))
    return _ok(enabled=enabled, name=row['name'])


# ---------- 渠道模型 ----------
@bp.get('/models')
def list_models():
    channel_id = _int(request.args.get('channel_id'))
    sql = ('SELECT m.*, c.name AS channel_name, c.base_url, c.enabled AS channel_enabled '
           'FROM channel_models m JOIN channels c ON c.id=m.channel_id ')
    params = ()
    if channel_id:
        sql += 'WHERE m.channel_id=? '
        params = (channel_id,)
    sql += 'ORDER BY c.sort, m.sort, m.id'
    out = []
    for r in query_all(sql, params):
        d = dict(r)
        d['types'] = router.parse_types(r['types'])
        d['available'] = [t for t in d['types'] if t in AI_TYPES]
        out.append(d)
    return jsonify({'ok': True, 'data': out})


def _pick_channel_key(channel_id):
    """挑一个可用于 GET /models 的 Key：优先未冷却的，全部冷却时取最早解冻的。"""
    rows = [dict(k) for k in query_all(
        'SELECT * FROM channel_keys WHERE channel_id=? AND enabled=1 ORDER BY sort, id',
        (channel_id,))]
    if not rows:
        return None
    now = ts()
    alive = [k for k in rows if (k.get('disabled_until') or 0) <= now]
    if alive:
        return alive[0]
    rows.sort(key=lambda k: k.get('disabled_until') or 0)
    return rows[0]


def _suggest_types(model_name):
    """按模型名推断能力类型（导入时的默认值，前端可以再改）。"""
    n = (model_name or '').lower()
    if any(k in n for k in ('embedding', 'embed-', 'bge-', 'gte-', 'jina-embed')):
        return []                      # 向量模型不在六类能力里，默认不导入
    if any(k in n for k in ('dall-e', 'dalle', 'gpt-image', 'imagen', 'flux',
                            'stable-diffusion', 'sd3', 'sd-xl', 'midjourney',
                            'seedream', 'kolors', 'recraft', 'ideogram')):
        return ['gen']
    if any(k in n for k in ('sora', 'veo', 'kling', 'runway', 'hailuo', 'cogvideo',
                            'wan2', 'wanx', 'seedance', 'video-gen')):
        return ['video_gen']
    if any(k in n for k in ('ocr', 'text-recognition', 'paddleocr')):
        return ['ocr']
    if any(k in n for k in ('vision', '-vl', 'vl-', '_vl', '4o', 'omni', 'gemini',
                            'claude-3', 'claude-4', 'glm-4v', 'qwen-vl', 'internvl',
                            'llava', 'pixtral', 'gpt-4.1', 'gpt-5', 'multimodal')):
        return ['text', 'vision']      # 多模态模型通常也能处理纯文本
    return ['text']


def _extract_remote_models(payload):
    """兼容多种上游 /models 返回格式：{'data':[...]} / {'models':[...]} / 直接数组。"""
    items = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ('data', 'models', 'result', 'items'):
            val = payload.get(key)
            if isinstance(val, list):
                items = val
                break
            if isinstance(val, dict):
                inner = val.get('models') or val.get('data') or val.get('items')
                if isinstance(inner, list):
                    items = inner
                    break
    out, seen = [], set()
    for item in items:
        if isinstance(item, str):
            name, owner = item, ''
        elif isinstance(item, dict):
            name = (item.get('id') or item.get('model') or item.get('name')
                    or item.get('model_name'))
            owner = item.get('owned_by') or item.get('owner') or item.get('provider')
        else:
            continue
        name = str(name or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({'id': name, 'owned_by': str(owner or '')[:80]})
    return out


def _fetch_remote_models(channel, key):
    """GET 上游 /models。Base URL 没写版本号时，自动再补一次 /v1/models。"""
    base = (channel['base_url'] or '').rstrip('/')
    urls = [base + '/models']
    if not re.search(r'/v\d+$', base):
        urls.append(base + '/v1/models')
    headers = {'Accept': 'application/json'}
    if key and key.get('api_key'):
        headers['Authorization'] = 'Bearer %s' % key['api_key']
    last_error = '无法获取上游模型列表'
    for url in urls:
        try:
            resp = router._http().get(url, headers=headers, timeout=(5, 30))
        except Exception as exc:
            last_error = '连接上游失败：%s' % exc
            continue
        if resp.status_code == 404 and url != urls[-1]:
            last_error = 'HTTP 404（%s）' % url
            continue
        if resp.status_code != 200:
            raise ValueError('上游返回 HTTP %s：%s'
                             % (resp.status_code, (resp.text or '')[:200]))
        try:
            payload = resp.json()
        except Exception:
            raise ValueError('上游返回的不是合法 JSON：%s' % (resp.text or '')[:200])
        return _extract_remote_models(payload), url
    raise ValueError(last_error)


@bp.get('/channels/<int:cid>/remote-models')
def channel_remote_models(cid):
    """读取上游模型列表，并按模型名给出建议类型与「是否已导入」标记。"""
    channel = query_one('SELECT * FROM channels WHERE id=?', (cid,))
    if not channel:
        return jsonify({'error': '渠道不存在'}), 404
    key = _pick_channel_key(cid)
    try:
        items, endpoint = _fetch_remote_models(channel, key)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 502

    local = {r['model_name']: {'types': router.parse_types(r['types']),
                               'enabled': int(r['enabled'])}
             for r in query_all('SELECT model_name, types, enabled FROM channel_models '
                                'WHERE channel_id=?', (cid,))}
    data = []
    for item in items:
        info = local.get(item['id'])
        data.append({
            'id': item['id'],
            'owned_by': item['owned_by'],
            'exists': info is not None,
            'enabled': bool(info['enabled']) if info else False,
            'suggested_types': (info['types'] if info else _suggest_types(item['id'])),
        })
    return jsonify({
        'ok': True,
        'data': data,
        'count': len(data),
        'key_label': (key or {}).get('label') or 'nokey',
        'endpoint': endpoint,
    })


@bp.post('/channels/<int:cid>/remote-models/import')
def import_remote_models(cid):
    """批量导入上游模型：已存在的更新类型，未存在的创建。"""
    if not query_one('SELECT id FROM channels WHERE id=?', (cid,)):
        return jsonify({'error': '渠道不存在'}), 404
    data = _payload()
    items = data.get('models')
    if not isinstance(items, list) or not items:
        return jsonify({'error': '请勾选要导入的模型'}), 400
    now = ts()
    created = updated = disabled = skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        name = str(item.get('model_name') or item.get('id') or '').strip()
        types = _clean_types(item.get('types'))
        enabled = _bool(item.get('enabled'), 1)
        if not name:
            skipped += 1
            continue
        row = query_one('SELECT id FROM channel_models WHERE channel_id=? AND model_name=?',
                        (cid, name))
        fields = {'enabled': enabled, 'updated_at': now}
        if types:
            fields['types'] = json.dumps(types, ensure_ascii=False)
        if item.get('priority') is not None:
            fields['priority'] = _int(item.get('priority'), 100)
        if item.get('weight') is not None:
            fields['weight'] = max(1, _int(item.get('weight'), 100))
        if row:
            update('channel_models', fields, 'id=?', (row['id'],))
            if enabled:
                updated += 1
            else:
                disabled += 1          # 取消勾选 = 停用，类型保留
        else:
            if not types:
                skipped += 1           # 新模型必须有类型才创建
                continue
            fields.update({'channel_id': cid, 'model_name': name[:120],
                           'sort': 0, 'priority': fields.get('priority', 100),
                           'weight': fields.get('weight', 100), 'timeout': 0,
                           'remark': '', 'created_at': now})
            insert('channel_models', fields)
            created += 1
    return _ok(created=created, updated=updated, disabled=disabled, skipped=skipped)


def _read_model(data):
    name = str(data.get('model_name') or '').strip()
    if not name:
        raise ValueError('模型名不能为空')
    types = _clean_types(data.get('types'))
    if not types:
        raise ValueError('至少勾选一种能力类型')
    return {
        'model_name': name[:120],
        'types': json.dumps(types, ensure_ascii=False),
        'enabled': _bool(data.get('enabled'), 1),
        'sort': _int(data.get('sort')),
        'priority': _int(data.get('priority'), 100),
        'weight': max(1, _int(data.get('weight'), 100)),
        'timeout': max(0, _int(data.get('timeout'), 0)),
        'remark': str(data.get('remark') or '')[:300],
        'updated_at': ts(),
    }


@bp.post('/models')
def create_model():
    data = _payload()
    cid = _int(data.get('channel_id'))
    if not query_one('SELECT id FROM channels WHERE id=?', (cid,)):
        return jsonify({'error': '请选择渠道'}), 400
    try:
        fields = _read_model(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if query_one('SELECT id FROM channel_models WHERE channel_id=? AND model_name=?',
                 (cid, fields['model_name'])):
        return jsonify({'error': '同渠道下该模型名已存在'}), 409
    fields['channel_id'] = cid
    fields['created_at'] = ts()
    mid = insert('channel_models', fields)
    return _ok(id=mid), 201


@bp.put('/models/<int:mid>')
def update_model(mid):
    row = query_one('SELECT * FROM channel_models WHERE id=?', (mid,))
    if not row:
        return jsonify({'error': '模型不存在'}), 404
    data = _payload()
    try:
        fields = _read_model(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    dup = query_one('SELECT id FROM channel_models WHERE channel_id=? AND model_name=? AND id<>?',
                    (row['channel_id'], fields['model_name'], mid))
    if dup:
        return jsonify({'error': '同渠道下该模型名已存在'}), 409
    update('channel_models', fields, 'id=?', (mid,))
    return _ok()


@bp.delete('/models/<int:mid>')
def delete_model(mid):
    execute('DELETE FROM channel_models WHERE id=?', (mid,))
    return _ok()


@bp.post('/models/<int:mid>/toggle')
def toggle_model(mid):
    """快速启用/停用模型（类型与其它配置全部保留）。"""
    row = query_one('SELECT id, model_name, enabled FROM channel_models WHERE id=?', (mid,))
    if not row:
        return jsonify({'error': '模型不存在'}), 404
    enabled = 0 if row['enabled'] else 1
    update('channel_models', {'enabled': enabled, 'updated_at': ts()}, 'id=?', (mid,))
    return _ok(enabled=enabled, model_name=row['model_name'])


# ---------- 连通性测试 ----------
def _test_payload(mtype, video_url=''):
    """六类能力的测试案例统一在 services/testcases 里维护。"""
    return testcases.build(mtype, video_url)


def _test_summary(mtype, data):
    if mtype in ('text', 'vision', 'ocr', 'video_vision'):
        try:
            return (data['choices'][0]['message']['content'] or '').strip()[:400]
        except Exception:
            return ''
    if mtype == 'gen':
        try:
            item = (data.get('data') or [{}])[0]
            if item.get('url'):
                return item['url']
            b64 = item.get('b64_json') or ''
            return 'b64_json（%d 字符）' % len(b64) if b64 else '已返回但无图片地址'
        except Exception:
            return ''
    return json.dumps(data, ensure_ascii=False)[:400]


def _run_test(model_id, mtype, video_url=''):
    mtype = mtype if mtype in AI_TYPES else 'text'
    try:
        endpoint, payload = _test_payload(mtype, video_url)
    except ValueError as e:
        return {'ok': False, 'type': mtype, 'error': str(e)}
    started = time.time()
    try:
        resp, cand, elapsed, attempts = router.request_single(
            model_id, mtype, endpoint, payload, timeout=60)
    except router.RouteError as e:
        return {'ok': False, 'type': mtype, 'error': e.message,
                'attempts': e.attempts,
                'elapsed_ms': int((time.time() - started) * 1000)}
    try:
        data = json.loads(resp.content.decode('utf-8', 'ignore'))
    except Exception:
        data = {}
    return {'ok': True, 'type': mtype, 'elapsed_ms': elapsed,
            'channel': cand['channel_name'], 'model': cand['model'],
            'key': cand['key_label'], 'reply': _test_summary(mtype, data),
            'usage': (data or {}).get('usage') or {}}


@bp.post('/models/<int:mid>/test')
def test_model(mid):
    data = _payload()
    mtype = str(data.get('type') or '').strip()
    row = query_one('SELECT * FROM channel_models WHERE id=?', (mid,))
    if not row:
        return jsonify({'error': '模型不存在'}), 404
    types = router.parse_types(row['types'])
    if not mtype or mtype not in types:
        mtype = types[0] if types else 'text'
    return jsonify(_run_test(mid, mtype, str(data.get('video_url') or '')))


@bp.post('/channels/<int:cid>/test')
def test_channel(cid):
    rows = query_all('SELECT * FROM channel_models WHERE channel_id=? AND enabled=1 '
                     'ORDER BY sort, id', (cid,))
    results = []
    for m in rows[:4]:
        types = router.parse_types(m['types'])
        results.append(dict(_run_test(m['id'], types[0] if types else 'text'),
                            model_id=m['id'], model_name=m['model_name']))
    return jsonify({'ok': True, 'data': results})


@bp.post('/channels/<int:cid>/test-model')
def test_remote_model(cid):
    """按「渠道 + 上游模型名」测试勾选的能力类型，模型无需先导入。

    返回每种类型的结果（ok / 耗时 / 回复或错误），前端据此给类型标签着色。
    测试不写 Key/渠道健康状态，不会影响正常调度。
    """
    data = _payload()
    model_name = str(data.get('model_name') or '').strip()
    types = _clean_types(data.get('types'))
    video_url = str(data.get('video_url') or '')
    if not model_name:
        return jsonify({'error': '缺少模型名'}), 400
    if not types:
        return jsonify({'error': '请先勾选要测试的能力类型'}), 400
    if not query_one('SELECT id FROM channels WHERE id=?', (cid,)):
        return jsonify({'error': '渠道不存在'}), 404

    results = []
    for mtype in types:
        started = time.time()
        try:
            endpoint, payload = testcases.build(mtype, video_url)
        except ValueError as exc:
            results.append({'type': mtype, 'ok': False, 'error': str(exc),
                            'elapsed_ms': 0})
            continue
        try:
            resp, cand, elapsed, attempts = router.request_by_name(
                cid, model_name, mtype, endpoint, payload, timeout=60)
        except router.RouteError as exc:
            results.append({'type': mtype, 'ok': False, 'error': exc.message,
                            'elapsed_ms': int((time.time() - started) * 1000),
                            'attempts': exc.attempts})
            continue
        try:
            body = json.loads(resp.content.decode('utf-8', 'ignore'))
        except Exception:
            body = {}
        results.append({
            'type': mtype,
            'ok': True,
            'elapsed_ms': elapsed,
            'reply': _test_summary(mtype, body),
            'usage': (body or {}).get('usage') or {},
        })
    return jsonify({'ok': True, 'data': results, 'model': model_name})


# ---------- 自定义模型（对外调度组） ----------
def _targets_of(cm_id):
    rows = query_all(
        'SELECT t.*, m.model_name, m.types, m.enabled AS model_enabled, '
        'c.name AS channel_name, c.enabled AS channel_enabled '
        'FROM custom_model_targets t '
        'JOIN channel_models m ON m.id=t.channel_model_id '
        'JOIN channels c ON c.id=m.channel_id '
        'WHERE t.custom_model_id=? ORDER BY t.priority, t.sort', (cm_id,))
    out = []
    for r in rows:
        d = dict(r)
        d['types'] = router.parse_types(r['types'])
        out.append(d)
    return out


@bp.get('/custom-models')
def list_custom_models():
    out = []
    for cm in query_all('SELECT * FROM custom_models ORDER BY id'):
        d = dict(cm)
        d['targets'] = _targets_of(cm['id'])
        out.append(d)
    return jsonify({'ok': True, 'data': out})


def _save_targets(cm_id, targets):
    existing = {t['channel_model_id']: dict(t) for t in
                query_all('SELECT * FROM custom_model_targets WHERE custom_model_id=?', (cm_id,))}
    keep = set()
    for idx, item in enumerate(targets or []):
        if not isinstance(item, dict):
            continue
        mid = _int(item.get('channel_model_id'))
        if not mid or not query_one('SELECT id FROM channel_models WHERE id=?', (mid,)):
            continue
        enabled = _bool(item.get('enabled', 1))
        priority = _int(item.get('priority'), 100)
        weight = max(1, _int(item.get('weight'), 100))
        if mid in existing:
            execute('UPDATE custom_model_targets SET enabled=?, priority=?, weight=?, sort=? '
                    'WHERE custom_model_id=? AND channel_model_id=?',
                    (enabled, priority, weight, idx, cm_id, mid))
        else:
            insert('custom_model_targets', {
                'custom_model_id': cm_id, 'channel_model_id': mid, 'enabled': enabled,
                'priority': priority, 'weight': weight, 'sort': idx, 'created_at': ts(),
            })
        keep.add(mid)
    for mid in existing:
        if mid not in keep:
            execute('DELETE FROM custom_model_targets WHERE custom_model_id=? '
                    'AND channel_model_id=?', (cm_id, mid))


def _read_custom(data):
    name = str(data.get('name') or '').strip()
    if not name:
        raise ValueError('模型名不能为空（客户端用它作为 model 参数）')
    if len(name) > 64:
        raise ValueError('模型名最长 64 个字符')
    if any(c in name for c in ' \t\r\n/\\?#'):
        raise ValueError('模型名不能包含空格或 / \\ ? # 等字符')
    strategy = str(data.get('strategy') or 'priority')
    if strategy not in ('priority', 'weighted'):
        strategy = 'priority'
    return {
        'name': name,
        'display_name': str(data.get('display_name') or '')[:80],
        'description': str(data.get('description') or '')[:500],
        'strategy': strategy,
        'enabled': _bool(data.get('enabled'), 1),
        'updated_at': ts(),
    }


@bp.post('/custom-models')
def create_custom_model():
    data = _payload()
    try:
        fields = _read_custom(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if query_one('SELECT id FROM custom_models WHERE name=?', (fields['name'],)):
        return jsonify({'error': '该模型名已存在'}), 409
    fields['created_at'] = ts()
    cm_id = insert('custom_models', fields)
    _save_targets(cm_id, data.get('targets'))
    return _ok(id=cm_id), 201


@bp.put('/custom-models/<int:cm_id>')
def update_custom_model(cm_id):
    row = query_one('SELECT * FROM custom_models WHERE id=?', (cm_id,))
    if not row:
        return jsonify({'error': '自定义模型不存在'}), 404
    data = _payload()
    try:
        fields = _read_custom(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    dup = query_one('SELECT id FROM custom_models WHERE name=? AND id<>?',
                    (fields['name'], cm_id))
    if dup:
        return jsonify({'error': '该模型名已存在'}), 409
    update('custom_models', fields, 'id=?', (cm_id,))
    if 'targets' in data:
        _save_targets(cm_id, data.get('targets'))
    return _ok()


@bp.delete('/custom-models/<int:cm_id>')
def delete_custom_model(cm_id):
    execute('DELETE FROM custom_models WHERE id=?', (cm_id,))
    return _ok()


# ---------- 知识库 ----------
def _kb_stats_sql():
    return ('SELECT k.*, '
            '(SELECT COUNT(*) FROM kb_documents d WHERE d.kb_id=k.id) AS doc_count, '
            '(SELECT COUNT(*) FROM kb_chunks c WHERE c.kb_id=k.id) AS chunk_count '
            'FROM knowledge_bases k ')


@bp.get('/kbs')
def list_kbs():
    rows = query_all(_kb_stats_sql() + 'ORDER BY k.id')
    return jsonify({'ok': True, 'data': [dict(r) for r in rows]})


def _read_kb(data):
    name = str(data.get('name') or '').strip()
    if not name:
        raise ValueError('知识库名称不能为空')
    chunk_size = max(80, _int(data.get('chunk_size'), DEFAULT_CHUNK_SIZE))
    overlap = _int(data.get('chunk_overlap'), DEFAULT_CHUNK_OVERLAP)
    return {
        'name': name[:80],
        'description': str(data.get('description') or '')[:500],
        'chunk_size': chunk_size,
        'chunk_overlap': max(0, min(overlap, chunk_size // 2)),
        'top_k': max(1, min(20, _int(data.get('top_k'), DEFAULT_TOP_K))),
        'min_score': max(0.0, min(1.0, _float(data.get('min_score'), DEFAULT_MIN_SCORE))),
        'enabled': _bool(data.get('enabled'), 1),
        'updated_at': ts(),
    }


@bp.post('/kbs')
def create_kb():
    data = _payload()
    try:
        fields = _read_kb(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    fields['created_at'] = ts()
    kb_id = insert('knowledge_bases', fields)
    return _ok(id=kb_id), 201


@bp.put('/kbs/<int:kb_id>')
def update_kb(kb_id):
    if not query_one('SELECT id FROM knowledge_bases WHERE id=?', (kb_id,)):
        return jsonify({'error': '知识库不存在'}), 404
    data = _payload()
    try:
        fields = _read_kb(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    update('knowledge_bases', fields, 'id=?', (kb_id,))
    if any(k in data for k in ('chunk_size', 'chunk_overlap')):
        knowledge.reindex_kb(kb_id)
    return _ok()


@bp.delete('/kbs/<int:kb_id>')
def delete_kb(kb_id):
    execute('DELETE FROM knowledge_bases WHERE id=?', (kb_id,))
    return _ok()


@bp.get('/kbs/<int:kb_id>/documents')
def list_documents(kb_id):
    rows = query_all('SELECT id, kb_id, title, source, char_count, chunk_count, created_at, '
                     'substr(content, 1, 160) AS preview '
                     'FROM kb_documents WHERE kb_id=? ORDER BY id DESC', (kb_id,))
    return jsonify({'ok': True, 'data': [dict(r) for r in rows]})


@bp.post('/kbs/<int:kb_id>/documents')
def upload_document(kb_id):
    if not query_one('SELECT id FROM knowledge_bases WHERE id=?', (kb_id,)):
        return jsonify({'error': '知识库不存在'}), 404
    upload = request.files.get('file')
    if upload is not None and upload.filename:
        raw = upload.read()
        if len(raw) > 20 * 1024 * 1024:
            return jsonify({'error': '文件超过 20MB 限制'}), 413
        try:
            content = knowledge.extract_text(upload.filename, raw)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        title = (request.form.get('title') or upload.filename).strip()
        source = 'file:' + upload.filename
    else:
        data = _payload()
        content = str(data.get('content') or '')
        title = str(data.get('title') or '').strip() or ('粘贴文档 ' + time.strftime('%Y-%m-%d %H:%M'))
        source = 'paste'
    if not content.strip():
        return jsonify({'error': '文档内容为空'}), 400
    doc_id, chunks = knowledge.add_document(kb_id, title, content, source)
    return jsonify({'ok': True, 'doc_id': doc_id, 'chunks': chunks,
                    'chars': len(content)})


@bp.get('/documents/<int:doc_id>')
def get_document(doc_id):
    row = query_one('SELECT * FROM kb_documents WHERE id=?', (doc_id,))
    if not row:
        return jsonify({'error': '文档不存在'}), 404
    return jsonify({'ok': True, 'data': dict(row)})


@bp.put('/documents/<int:doc_id>')
def update_document(doc_id):
    """编辑文档内容/标题，保存后按知识库当前参数重新切片并重建向量。"""
    if not query_one('SELECT id FROM kb_documents WHERE id=?', (doc_id,)):
        return jsonify({'error': '文档不存在'}), 404
    data = _payload()
    try:
        chunks = knowledge.update_document(
            doc_id, str(data.get('title') or '').strip(), str(data.get('content') or ''))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return _ok(chunks=chunks)


@bp.delete('/documents/<int:doc_id>')
def delete_document(doc_id):
    knowledge.delete_document(doc_id)
    return _ok()


@bp.post('/kbs/<int:kb_id>/reindex')
def reindex_kb(kb_id):
    try:
        docs, chunks = knowledge.reindex_kb(kb_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    return _ok(docs=docs, chunks=chunks)


@bp.post('/kbs/<int:kb_id>/search')
def search_kb(kb_id):
    data = _payload()
    query = str(data.get('query') or '').strip()
    if not query:
        return jsonify({'error': '请输入检索内容'}), 400
    kb = query_one('SELECT * FROM knowledge_bases WHERE id=?', (kb_id,))
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404
    started = time.time()
    hits = knowledge.search([kb_id], query, _int(data.get('top_k'), kb['top_k']),
                            _float(data.get('min_score'), kb['min_score']))
    return jsonify({'ok': True, 'data': hits,
                    'elapsed_ms': int((time.time() - started) * 1000)})


# ---------- 站内 API Key ----------
def _key_detail(row, with_secret=True):
    d = dict(row)
    kbs = query_all('SELECT kb_id FROM api_key_kbs WHERE api_key_id=?', (row['id'],))
    d['kb_ids'] = [r['kb_id'] for r in kbs]
    if not with_secret:
        d.pop('key', None)
    return d


@bp.get('/api-keys')
def list_api_keys():
    rows = query_all('SELECT * FROM api_keys ORDER BY id DESC')
    return jsonify({'ok': True, 'data': [_key_detail(r) for r in rows]})


def _gen_key():
    return 'sk-ou-' + secrets.token_hex(24)


def _save_key_kbs(key_id, kb_ids):
    execute('DELETE FROM api_key_kbs WHERE api_key_id=?', (key_id,))
    for kb_id in kb_ids or []:
        kb_id = _int(kb_id)
        if kb_id and query_one('SELECT id FROM knowledge_bases WHERE id=?', (kb_id,)):
            execute('INSERT OR IGNORE INTO api_key_kbs (api_key_id, kb_id) VALUES (?,?)',
                    (key_id, kb_id))


@bp.post('/api-keys')
def create_api_key():
    data = _payload()
    name = str(data.get('name') or '').strip() or ('Key ' + time.strftime('%m-%d %H:%M'))
    token = _gen_key()
    key_id = insert('api_keys', {
        'name': name[:80], 'key': token, 'prefix': token[:12],
        'enabled': _bool(data.get('enabled'), 1),
        'rpm_limit': max(0, _int(data.get('rpm_limit'), 0)),
        'notes': str(data.get('notes') or '')[:300],
        'created_at': ts(),
    })
    _save_key_kbs(key_id, data.get('kb_ids'))
    return jsonify({'ok': True, 'id': key_id, 'key': token}), 201


@bp.put('/api-keys/<int:key_id>')
def update_api_key(key_id):
    if not query_one('SELECT id FROM api_keys WHERE id=?', (key_id,)):
        return jsonify({'error': 'API Key 不存在'}), 404
    data = _payload()
    fields = {
        'name': str(data.get('name') or '')[:80],
        'enabled': _bool(data.get('enabled'), 1),
        'rpm_limit': max(0, _int(data.get('rpm_limit'), 0)),
        'notes': str(data.get('notes') or '')[:300],
    }
    update('api_keys', fields, 'id=?', (key_id,))
    if 'kb_ids' in data:
        _save_key_kbs(key_id, data.get('kb_ids'))
    return _ok()


@bp.delete('/api-keys/<int:key_id>')
def delete_api_key(key_id):
    execute('DELETE FROM api_keys WHERE id=?', (key_id,))
    return _ok()


@bp.post('/api-keys/<int:key_id>/rotate')
def rotate_api_key(key_id):
    if not query_one('SELECT id FROM api_keys WHERE id=?', (key_id,)):
        return jsonify({'error': 'API Key 不存在'}), 404
    token = _gen_key()
    execute('UPDATE api_keys SET key=?, prefix=? WHERE id=?', (token, token[:12], key_id))
    return _ok(key=token)


# ---------- 调用日志 ----------
@bp.get('/logs')
def list_logs():
    page = max(1, _int(request.args.get('page'), 1))
    size = min(200, max(10, _int(request.args.get('size'), 50)))
    where, params = [], []
    ok = request.args.get('ok')
    if ok in ('0', '1'):
        where.append('ok=?')
        params.append(int(ok))
    rtype = request.args.get('type')
    if rtype:
        where.append('resolved_type=?')
        params.append(rtype)
    q = (request.args.get('q') or '').strip()
    if q:
        where.append('(request_model LIKE ? OR channel_name LIKE ? OR error LIKE ? '
                     'OR api_key_name LIKE ? OR upstream_model LIKE ?)')
        params.extend(['%' + q + '%'] * 5)
    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = query_one('SELECT COUNT(*) AS n FROM call_logs ' + clause, params)['n']
    rows = query_all('SELECT * FROM call_logs ' + clause + ' ORDER BY id DESC LIMIT ? OFFSET ?',
                     params + [size, (page - 1) * size])
    return jsonify({'ok': True, 'total': total, 'page': page, 'size': size,
                    'data': [dict(r) for r in rows]})


@bp.delete('/logs')
def clear_logs():
    execute('DELETE FROM call_logs')
    return _ok()


# ---------- 系统设置 ----------
@bp.get('/settings')
def get_settings():
    return jsonify({
        'ok': True,
        'data': {
            'rag_top_k': get_setting('rag_top_k', str(DEFAULT_TOP_K)),
            'rag_min_score': get_setting('rag_min_score', str(DEFAULT_MIN_SCORE)),
            'rag_max_chars': get_setting('rag_max_chars', '3000'),
            'rag_enabled': get_setting('rag_enabled', '1'),
            'vector_dim': VECTOR_DIM,
            'password_is_default': get_setting('admin_password_default') == '1',
        },
    })


@bp.put('/settings')
def save_settings():
    data = _payload()
    if 'rag_top_k' in data:
        set_setting('rag_top_k', max(1, min(20, _int(data.get('rag_top_k'), DEFAULT_TOP_K))))
    if 'rag_min_score' in data:
        set_setting('rag_min_score',
                    max(0.0, min(1.0, _float(data.get('rag_min_score'), DEFAULT_MIN_SCORE))))
    if 'rag_max_chars' in data:
        set_setting('rag_max_chars', max(200, min(12000, _int(data.get('rag_max_chars'), 3000))))
    if 'rag_enabled' in data:
        set_setting('rag_enabled', '1' if _bool(data.get('rag_enabled'), 1) else '0')
    return _ok()


# ---------- 类型定义（前端渲染勾选框） ----------
@bp.get('/ai-types')
def ai_types():
    avail = router.available_types()
    return jsonify({'ok': True, 'data': [
        {'key': t, 'label': AI_TYPES[t]['label'], 'desc': AI_TYPES[t]['desc'],
         'icon': AI_TYPES[t]['icon'], 'available': avail.get(t, False)}
        for t in AI_TYPE_ORDER]})


# ---------- 文档 AI 润色 ----------
_POLISH_PROMPT = (
    '你是知识库文档整理助手。把用户提供的原始文档整理成「适合向量检索」的格式。\n'
    '要求：\n'
    '1. 保留全部事实、数字、日期、金额、专有名词，不得编造、不得省略关键信息；\n'
    '2. 按主题拆成若干段落，每段以简短小标题开头（如「退款条件：」）；\n'
    '3. 每段尽量自包含，避免「上述」「该规定」「其」这类依赖上下文的指代，必要时重复关键主语；\n'
    '4. 把口语、广告语、重复内容改写为简洁陈述句，保留原文的列表与步骤顺序；\n'
    '5. 只输出整理后的正文，不要 markdown 代码块、不要解释、不要评价、不要额外总结。\n'
)


def _strip_fence(text):
    text = (text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z0-9]*\s*', '', text)
        text = re.sub(r'\s*```$', '', text).strip()
    return text


def _polish_once(model_name, title, content):
    """调用一次模型润色一段文本，返回 (润色后文本, 候选, 耗时)。"""
    payload = {
        'messages': [
            {'role': 'system', 'content': _POLISH_PROMPT},
            {'role': 'user', 'content': '文档标题：%s\n\n原始内容：\n%s' % (title, content)},
        ],
        'temperature': 0.2,
        'max_tokens': 4000,
    }
    resp, cand, elapsed, attempts = router.request_upstream(
        'text', '/chat/completions', payload, model_name=model_name or None, timeout=180)
    try:
        body = json.loads(resp.content.decode('utf-8', 'ignore'))
        text = _strip_fence(body['choices'][0]['message']['content'] or '')
    except Exception:
        raise ValueError('上游返回格式无法解析')
    if not text:
        raise ValueError('模型没有返回内容')
    return text, cand, elapsed


@bp.post('/ai/polish-document')
def polish_document():
    """用指定模型把文档润色成更适合建立索引的格式（只返回文本，不直接保存）。

    超过 RAG_POLISH_BATCH_CHARS 的文档会自动按句子分批，逐批润色后拼接，
    这样单次可以处理到 RAG_POLISH_MAX_CHARS（默认 60000 字），不会被模型上下文卡住。
    """
    data = _payload()
    content = str(data.get('content') or '').strip()
    title = str(data.get('title') or '').strip()
    model_name = str(data.get('model') or '').strip()
    if not content:
        return jsonify({'error': '文档内容为空'}), 400
    if len(content) > RAG_POLISH_MAX_CHARS:
        return jsonify({'error': '文档超过 %d 字，请先拆分后再润色'
                                  % RAG_POLISH_MAX_CHARS}), 400
    if model_name:
        cm = query_one('SELECT id, enabled FROM custom_models WHERE name=?', (model_name,))
        if cm:
            if not cm['enabled']:
                return jsonify({'error': '模型 %s 已停用' % model_name}), 400
        elif not query_one('SELECT id FROM channel_models WHERE model_name=? AND enabled=1',
                           (model_name,)):
            return jsonify({'error': '模型不存在：%s' % model_name}), 400

    parts = knowledge.split_text(content, RAG_POLISH_BATCH_CHARS, 200)
    if not parts:
        return jsonify({'error': '文档内容为空'}), 400
    if len(parts) > RAG_POLISH_MAX_BATCHES:
        return jsonify({'error': '文档过长（需要分 %d 批处理，上限 %d 批），请先拆分'
                                  % (len(parts), RAG_POLISH_MAX_BATCHES)}), 400

    polished, channels, total_ms = [], [], 0
    last_cand = None
    for idx, part in enumerate(parts):
        batch_title = title or '（无标题）'
        if len(parts) > 1:
            batch_title += '（第 %d/%d 部分，只整理本部分）' % (idx + 1, len(parts))
        try:
            text, cand, elapsed = _polish_once(model_name, batch_title, part)
        except router.RouteError as exc:
            return jsonify({'error': '第 %d/%d 批失败：%s'
                                      % (idx + 1, len(parts), exc.message),
                            'attempts': exc.attempts}), 502
        except ValueError as exc:
            return jsonify({'error': '第 %d/%d 批失败：%s' % (idx + 1, len(parts), exc)}), 502
        polished.append(text)
        channels.append(cand['channel_name'])
        total_ms += elapsed
        last_cand = cand

    return _ok(content='\n\n'.join(polished),
               model=last_cand['model'],
               channel='、'.join(dict.fromkeys(channels)),
               elapsed_ms=total_ms,
               batches=len(parts))


# ---------- Playground ----------
@bp.post('/playground')
def playground():
    """后台在线测试：直接用自定义模型名（或真实模型名）发一次 chat。"""
    data = _payload()
    model = str(data.get('model') or '').strip()
    prompt = str(data.get('prompt') or '').strip()
    image = str(data.get('image') or '').strip()
    use_kb = _bool(data.get('use_kb'), 0)
    if not model:
        return jsonify({'error': '请选择模型'}), 400
    if not prompt and not image:
        return jsonify({'error': '请输入内容'}), 400
    content = [{'type': 'text', 'text': prompt or '请描述这张图片。'}]
    if image:
        part = router.image_part(image)
        if part:
            content.append(part)
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': content if image else (prompt or '你好')}],
        'max_tokens': max(16, min(4096, _int(data.get('max_tokens'), 512))),
        'temperature': _float(data.get('temperature'), 0.7),
    }
    mtype = router.detect_chat_type(body, str(data.get('task_type') or '') or None)
    kb_ids = []
    if use_kb:
        kb_ids = [_int(x) for x in (data.get('kb_ids') or []) if _int(x)]
        if not kb_ids:
            kb_ids = [r['id'] for r in query_all('SELECT id FROM knowledge_bases WHERE enabled=1')]
        body['messages'], hits = knowledge.rag_enrich(body['messages'], kb_ids)
    else:
        hits = []
    try:
        resp, cand, elapsed, attempts = router.request_upstream(
            mtype, '/chat/completions', body, model_name=model, timeout=120)
    except router.RouteError as e:
        return jsonify({'ok': False, 'error': e.message, 'attempts': e.attempts}), 502
    try:
        payload = json.loads(resp.content.decode('utf-8', 'ignore'))
    except Exception:
        payload = {}
    reply = ''
    try:
        reply = payload['choices'][0]['message']['content']
    except Exception:
        reply = json.dumps(payload, ensure_ascii=False)[:1000]
    return jsonify({'ok': True, 'type': mtype, 'elapsed_ms': elapsed,
                    'channel': cand['channel_name'], 'model': cand['model'],
                    'reply': reply, 'hits': hits,
                    'usage': payload.get('usage') or {}})
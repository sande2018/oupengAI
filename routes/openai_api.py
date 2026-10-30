# -*- coding: utf-8 -*-
"""OpenAI 兼容 API（/v1/*）。

对外只暴露一个「自定义模型名」，内部按能力类型自动选渠道：
  - chat/completions：按 messages 内容判定 text / vision / ocr / video_vision；
  - images/generations：gen；
  - videos/generations：video_gen（端点路径按渠道配置）；
  - embeddings：本地哈希向量（离线，不消耗上游额度）；
  - models：列出可用的自定义模型与上游真实模型名。
站内 API Key 关联了知识库时，chat 请求会自动检索并把资料注入 system。
"""
import json
import threading
import time
import uuid
from urllib.parse import quote

from flask import (Blueprint, Response, g, jsonify, request, session,
                   stream_with_context)

from config import (AI_TYPES, CHAT_TYPES, DEFAULT_MIN_SCORE, DEFAULT_TOP_K,
                    VECTOR_DIM)
from db import execute, get_setting, insert, query_all, query_one, ts
from services import knowledge, router

bp = Blueprint('openai', __name__)

_rpm_lock = threading.Lock()
_rpm = {}          # api_key_id -> [请求时间戳]


# ---------- 通用工具 ----------
def _error(status, message, code=None, etype='invalid_request_error', extra=None):
    err = {'message': message, 'type': etype, 'code': code}
    if extra:
        err.update(extra)
    return jsonify({'error': err}), status


def _bearer():
    auth = request.headers.get('Authorization', '') or ''
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return (request.headers.get('X-Api-Key') or '').strip()


def _check_rpm(row):
    limit = int(row.get('rpm_limit') or 0)
    if limit <= 0:
        return True
    now = time.time()
    with _rpm_lock:
        arr = [t for t in _rpm.get(row['id'], []) if now - t < 60]
        if len(arr) >= limit:
            _rpm[row['id']] = arr
            return False
        arr.append(now)
        _rpm[row['id']] = arr
    return True


@bp.before_request
def _auth():
    if request.method == 'OPTIONS':
        return None
    api_key = None
    if session.get('admin'):
        g.api_key = None          # 后台 Playground 用登录态直接调用
        return None
    token = _bearer()
    if not token:
        return _error(401, '缺少 API Key：请使用 Authorization: Bearer sk-... 调用',
                      'missing_api_key')
    row = query_one('SELECT * FROM api_keys WHERE key=?', (token,))
    if not row:
        return _error(401, 'API Key 无效', 'invalid_api_key')
    if not row['enabled']:
        return _error(403, '该 API Key 已被停用', 'api_key_disabled')
    api_key = dict(row)
    if not _check_rpm(api_key):
        return _error(429, '请求过于频繁（超出该 Key 的每分钟限额）', 'rate_limit_exceeded')
    g.api_key = api_key
    execute('UPDATE api_keys SET total_calls=total_calls+1, last_used_at=? WHERE id=?',
            (ts(), api_key['id']))
    return None


def _write_log(request_id, endpoint, api_key, request_model, mtype, cand, hits,
               ok, status_code, latency_ms, usage=None, error=''):
    usage = usage or {}
    cand = cand or {}
    try:
        insert('call_logs', {
            'request_id': request_id,
            'endpoint': endpoint,
            'api_key_id': (api_key or {}).get('id', 0) or 0,
            'api_key_name': (api_key or {}).get('name', '') or '',
            'request_model': request_model or '',
            'resolved_type': cand.get('type') or (mtype or ''),
            'channel_id': cand.get('channel_id', 0) or 0,
            'channel_name': cand.get('channel_name', '') or '',
            'upstream_model': cand.get('model', '') or '',
            'key_label': cand.get('key_label', '') or '',
            'rag_hits': len(hits or []),
            'ok': 1 if ok else 0,
            'status_code': int(status_code or 0),
            'latency_ms': int(latency_ms or 0),
            'prompt_tokens': int(usage.get('prompt_tokens') or 0),
            'completion_tokens': int(usage.get('completion_tokens') or 0),
            'error': (error or '')[:1000],
            'created_at': ts(),
        })
    except Exception as exc:            # 日志失败绝不影响主流程
        print('[log] 写入调用日志失败：', exc)


def _add_usage(api_key, usage):
    if not api_key or not usage:
        return
    total = int(usage.get('total_tokens') or 0)
    if total <= 0:
        total = int(usage.get('prompt_tokens') or 0) + int(usage.get('completion_tokens') or 0)
    if total > 0:
        execute('UPDATE api_keys SET total_tokens=total_tokens+? WHERE id=?',
                (total, api_key['id']))


def _resolve_model(model_name):
    """校验模型名：自定义模型（调度组）或已配置的上游真实模型。返回 (custom_row, error)。"""
    if not model_name:
        return None, None
    custom = query_one('SELECT * FROM custom_models WHERE name=?', (model_name,))
    if custom:
        if not custom['enabled']:
            return None, _error(404, '模型 %s 已停用' % model_name, 'model_disabled')
        return dict(custom), None
    real = query_one('SELECT id FROM channel_models WHERE model_name=? AND enabled=1',
                     (model_name,))
    if not real:
        return None, _error(404, 'The model `%s` does not exist' % model_name,
                            'model_not_found')
    return None, None


def _kb_ids(knowledge_opt, api_key_row):
    """决定本次请求用哪些知识库：请求头 > 请求体扩展字段 > API Key 绑定。"""
    hdr = (request.headers.get('X-Knowledge') or '').strip().lower()
    if hdr in ('off', '0', 'false', 'no', 'none'):
        return []
    ids_hdr = request.headers.get('X-Knowledge-Ids') or ''
    if ids_hdr.strip():
        return [int(x) for x in ids_hdr.replace(',', ' ').split() if x.isdigit()]
    if isinstance(knowledge_opt, dict):
        if knowledge_opt.get('enabled') is False:
            return []
        ids = knowledge_opt.get('kb_ids')
        if isinstance(ids, list) and ids:
            return [int(i) for i in ids if str(i).isdigit()]
    elif knowledge_opt is False:
        return []
    if api_key_row:
        rows = query_all('SELECT a.kb_id FROM api_key_kbs a '
                         'JOIN knowledge_bases k ON k.id=a.kb_id '
                         'WHERE a.api_key_id=? AND k.enabled=1',
                         (api_key_row['id'],))
        return [r['kb_id'] for r in rows]
    return []


def _rag_opts(knowledge_opt):
    """只返回显式覆盖项。

    top_k / min_score 默认使用各知识库自己的配置，只有请求体里明确指定时才覆盖；
    max_chars 用系统设置作为全局上限，可在请求里临时调整。
    """
    opts = {'max_chars': int(get_setting('rag_max_chars', 3000) or 3000)}
    if isinstance(knowledge_opt, dict):
        if knowledge_opt.get('top_k') not in (None, ''):
            opts['top_k'] = int(knowledge_opt['top_k'])
        if knowledge_opt.get('min_score') not in (None, ''):
            opts['min_score'] = float(knowledge_opt['min_score'])
        if knowledge_opt.get('max_chars') not in (None, ''):
            opts['max_chars'] = int(knowledge_opt['max_chars'])
    return opts


def _resp_headers(cand, hits, started, extra=None):
    headers = {
        'X-Ou-Model': quote(str(cand.get('model') or ''), safe=''),
        'X-Ou-Channel': quote(str(cand.get('channel_name') or ''), safe=''),
        'X-Ou-Type': cand.get('type') or '',
        'X-Ou-Rag-Hits': str(len(hits or [])),
        'X-Ou-Latency-Ms': str(int((time.time() - started) * 1000)),
    }
    if extra:
        headers.update(extra)
    return headers


def _parse_body():
    """解析 JSON 请求体，并把站内扩展字段剥离出来（不能透传给上游）。

    返回 (body, ext)；body 为 None 表示请求体非法。注意 Flask 会缓存 get_json 的结果，
    所以这里必须一次解析、同时拿到扩展字段，不能分两次读。
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, None
    ext = {
        'knowledge': body.pop('knowledge', None),
        'task_type': body.pop('task_type', None),
    }
    body.pop('ou_meta', None)
    return body, ext


def _parse_json_body():
    """兼容旧调用：只要业务体，不要扩展字段。"""
    body, _ = _parse_body()
    return body


# ---------- 流式透传 ----------
def _scan_sse_event(raw, meta):
    for line in raw.split(b'\n'):
        line = line.strip()
        if not line.startswith(b'data:'):
            continue
        data = line[5:].strip()
        if not data or data == b'[DONE]':
            continue
        try:
            obj = json.loads(data.decode('utf-8', 'ignore'))
        except Exception:
            continue
        if isinstance(obj, dict):
            if obj.get('usage'):
                meta['usage'] = obj['usage']
            if obj.get('choices') and isinstance(obj['choices'], list):
                meta['chunks'] = meta.get('chunks', 0) + 1


def _proxy_stream(upstream, request_id, endpoint, api_key, model_name, cand, hits,
                  started):
    meta = {'usage': {}, 'interrupted': False}
    latency = int((time.time() - started) * 1000)

    def generate():
        buf = b''
        try:
            for chunk in upstream.iter_content(chunk_size=1024):
                if not chunk:
                    continue
                yield chunk
                buf += chunk
                while b'\n\n' in buf:
                    evt, buf = buf.split(b'\n\n', 1)
                    _scan_sse_event(evt, meta)
        except Exception as exc:
            meta['interrupted'] = True
            payload = {'error': {'message': '上游流式连接中断：%s' % exc,
                                 'type': 'upstream_error'}}
            yield ('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n').encode('utf-8')
        finally:
            try:
                upstream.close()
            except Exception:
                pass
            _write_log(request_id, endpoint, api_key, model_name,
                       cand.get('type'), cand, hits,
                       0 if meta['interrupted'] else 1,
                       502 if meta['interrupted'] else 200,
                       int((time.time() - started) * 1000), meta.get('usage'),
                       '' if not meta['interrupted'] else '流式连接中断')
            _add_usage(api_key, meta.get('usage'))

    headers = _resp_headers(cand, hits, started, {
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
        'X-Ou-Stream': '1',
    })
    return Response(stream_with_context(generate()), status=200,
                    mimetype='text/event-stream', headers=headers)


# ---------- chat/completions ----------
def _handle_chat():
    started = time.time()
    request_id = uuid.uuid4().hex[:12]
    body, ext = _parse_body()
    if body is None:
        return _error(400, '请求体必须是合法 JSON 对象', 'invalid_json')

    knowledge_opt = ext.get('knowledge')
    hint = request.headers.get('X-Task-Type') or ext.get('task_type')
    model_name = str(body.get('model') or '').strip()
    custom, err = _resolve_model(model_name)
    if err:
        return err
    mtype = router.detect_chat_type(body, hint)
    if mtype not in CHAT_TYPES:
        return _error(400, '该模型类型不走 chat 接口：%s' % mtype, 'invalid_task_type')

    messages = body.get('messages')
    if not isinstance(messages, list) or not messages:
        return _error(400, 'messages 不能为空', 'invalid_messages')

    api_key = g.get('api_key')
    kb_ids = _kb_ids(knowledge_opt, api_key)
    hits = []
    if kb_ids and mtype in ('text', 'vision'):
        messages, hits = knowledge.rag_enrich(messages, kb_ids, **_rag_opts(knowledge_opt))
        body['messages'] = messages

    stream = bool(body.get('stream'))
    try:
        resp, cand, elapsed, attempts = router.request_upstream(
            mtype, '/chat/completions', body, model_name=model_name, stream=stream,
            extra_headers={'Accept': 'text/event-stream'} if stream else None)
    except router.RouteError as e:
        _write_log(request_id, 'chat.completions', api_key, model_name, mtype,
                   e.attempts[-1] if e.attempts else None, hits, 0, e.status,
                   int((time.time() - started) * 1000), None, e.message)
        return Response(json.dumps(e.to_openai(), ensure_ascii=False),
                        status=e.status, content_type='application/json',
                        headers={'X-Ou-Type': mtype})

    if stream:
        return _proxy_stream(resp, request_id, 'chat.completions', api_key,
                             model_name, cand, hits, started)

    try:
        data = json.loads(resp.content.decode('utf-8', 'ignore'))
    except Exception:
        data = None
    if not isinstance(data, dict):
        _write_log(request_id, 'chat.completions', api_key, model_name, mtype, cand,
                   hits, 0, 502, int((time.time() - started) * 1000), None, '上游返回非 JSON')
        return _error(502, '上游返回了无法解析的响应', 'bad_upstream_response')

    if model_name:
        data['model'] = model_name
    usage = data.get('usage') or {}
    _write_log(request_id, 'chat.completions', api_key, model_name, mtype, cand, hits,
               1, 200, elapsed, usage)
    _add_usage(api_key, usage)
    out = json.dumps(data, ensure_ascii=False, indent=None).encode('utf-8')
    return Response(out, status=200, content_type='application/json',
                    headers=_resp_headers(cand, hits, started))


@bp.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    return _handle_chat()


# ---------- images/generations ----------
@bp.route('/v1/images/generations', methods=['POST'])
def images_generations():
    started = time.time()
    request_id = uuid.uuid4().hex[:12]
    body = _parse_json_body()
    if body is None:
        return _error(400, '请求体必须是合法 JSON 对象', 'invalid_json')
    model_name = str(body.get('model') or '').strip()
    _, err = _resolve_model(model_name)
    if err:
        return err
    timeout = body.pop('timeout', None)
    try:
        resp, cand, elapsed, attempts = router.request_upstream(
            'gen', '/images/generations', body, model_name=model_name or None,
            timeout=int(timeout) if timeout else None)
    except router.RouteError as e:
        _write_log(request_id, 'images.generations', g.get('api_key'), model_name,
                   'gen', e.attempts[-1] if e.attempts else None, [], 0, e.status,
                   int((time.time() - started) * 1000), None, e.message)
        return Response(json.dumps(e.to_openai(), ensure_ascii=False),
                        status=e.status, content_type='application/json',
                        headers={'X-Ou-Type': 'gen'})

    try:
        data = json.loads(resp.content.decode('utf-8', 'ignore'))
    except Exception:
        data = None
    if isinstance(data, dict) and model_name:
        data.setdefault('created', int(time.time()))
        data['model'] = model_name
    usage = (data or {}).get('usage') or {}
    _write_log(request_id, 'images.generations', g.get('api_key'), model_name, 'gen',
               cand, [], 1, 200, elapsed, usage)
    _add_usage(g.get('api_key'), usage)
    if data is None:
        return Response(resp.content, status=200, content_type='application/json',
                        headers=_resp_headers(cand, [], started))
    return Response(json.dumps(data, ensure_ascii=False).encode('utf-8'), status=200,
                    content_type='application/json',
                    headers=_resp_headers(cand, [], started))


# ---------- videos/generations（各家路径不同，按渠道配置） ----------
@bp.route('/v1/videos/generations', methods=['POST'])
@bp.route('/v1/video/generations', methods=['POST'])
def videos_generations():
    started = time.time()
    request_id = uuid.uuid4().hex[:12]
    body = _parse_json_body()
    if body is None:
        return _error(400, '请求体必须是合法 JSON 对象', 'invalid_json')
    model_name = str(body.get('model') or '').strip()
    _, err = _resolve_model(model_name)
    if err:
        return err
    timeout = body.pop('timeout', None)
    try:
        resp, cand, elapsed, attempts = router.request_upstream(
            'video_gen', None, body, model_name=model_name or None,
            timeout=int(timeout) if timeout else None)
    except router.RouteError as e:
        _write_log(request_id, 'videos.generations', g.get('api_key'), model_name,
                   'video_gen', e.attempts[-1] if e.attempts else None, [], 0, e.status,
                   int((time.time() - started) * 1000), None, e.message)
        return Response(json.dumps(e.to_openai(), ensure_ascii=False),
                        status=e.status, content_type='application/json',
                        headers={'X-Ou-Type': 'video_gen'})

    try:
        data = json.loads(resp.content.decode('utf-8', 'ignore'))
    except Exception:
        data = None
    if isinstance(data, dict) and model_name:
        data['model'] = model_name
    _write_log(request_id, 'videos.generations', g.get('api_key'), model_name,
               'video_gen', cand, [], 1, 200, elapsed, (data or {}).get('usage') or {})
    if data is None:
        return Response(resp.content, status=200, content_type='application/json',
                        headers=_resp_headers(cand, [], started))
    return Response(json.dumps(data, ensure_ascii=False).encode('utf-8'), status=200,
                    content_type='application/json',
                    headers=_resp_headers(cand, [], started))


# ---------- embeddings（本地向量，方便客户端自建索引） ----------
@bp.route('/v1/embeddings', methods=['POST'])
def embeddings():
    body = _parse_json_body()
    if body is None:
        return _error(400, '请求体必须是合法 JSON 对象', 'invalid_json')
    raw = body.get('input')
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list) and raw:
        items = []
        for x in raw:
            if isinstance(x, str):
                items.append(x)
            elif isinstance(x, list):
                items.append(' '.join(str(i) for i in x))
            else:
                items.append(str(x))
    else:
        return _error(400, 'input 必须是字符串或字符串数组', 'invalid_input')

    model_name = str(body.get('model') or 'oupeng-local-hash')
    data = [{'object': 'embedding', 'index': i, 'embedding': knowledge.embed(text)}
            for i, text in enumerate(items)]
    tokens = sum(len(t) for t in items)
    return jsonify({
        'object': 'list',
        'data': data,
        'model': model_name,
        'usage': {'prompt_tokens': tokens, 'total_tokens': tokens},
        '_meta': {'dimension': VECTOR_DIM, 'provider': 'local-hash'},
    })


# ---------- models ----------
def _model_obj(name, created, owner):
    return {'id': name, 'object': 'model', 'created': int(created or time.time()),
            'owned_by': owner, 'permission': []}


@bp.route('/v1/models', methods=['GET'])
def list_models():
    data, names = [], set()
    for cm in query_all('SELECT * FROM custom_models WHERE enabled=1 ORDER BY id'):
        data.append(_model_obj(cm['name'], cm['created_at'], 'oupeng'))
        names.add(cm['name'])
    rows = query_all(
        'SELECT m.model_name AS name, MIN(m.created_at) AS created '
        'FROM channel_models m JOIN channels c ON c.id=m.channel_id '
        'WHERE m.enabled=1 AND c.enabled=1 GROUP BY m.model_name ORDER BY m.model_name')
    for r in rows:
        if r['name'] in names:
            continue
        data.append(_model_obj(r['name'], r['created'], 'upstream'))
    return jsonify({'object': 'list', 'data': data})


@bp.route('/v1/models/<path:model_id>', methods=['GET'])
def model_detail(model_id):
    cm = query_one('SELECT * FROM custom_models WHERE name=?', (model_id,))
    if cm:
        return jsonify(_model_obj(cm['name'], cm['created_at'], 'oupeng'))
    row = query_one('SELECT model_name, created_at FROM channel_models '
                    'WHERE model_name=? LIMIT 1', (model_id,))
    if not row:
        return _error(404, 'The model `%s` does not exist' % model_id, 'model_not_found')
    return jsonify(_model_obj(row['model_name'], row['created_at'], 'upstream'))


# ---------- 能力说明（给前端/客户端看的能力矩阵） ----------
@bp.route('/v1/capabilities', methods=['GET'])
def capabilities():
    avail = router.available_types()
    return jsonify({
        'object': 'list',
        'data': [{'type': t, 'label': AI_TYPES[t]['label'], 'desc': AI_TYPES[t]['desc'],
                  'available': avail[t]} for t in router.AI_TYPE_ORDER],
    })
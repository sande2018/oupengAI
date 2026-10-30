# -*- coding: utf-8 -*-
"""测试用的假 OpenAI 上游。

按模型名 / Key 约定返回不同结果，用来验证调度、故障切换与 Key 轮换：
  - 模型名 fail500 → 500；fail400 → 400
  - Authorization 里含 k-bad → 401
  - 模型名 gen-model → 返回图片；video-model → 返回视频任务
"""
import json
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request


class MockUpstream:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()
        self.app = Flask('mock-upstream')
        self.app.json.ensure_ascii = False
        self._register()

    # ---------- 记录 ----------
    def record(self, endpoint, body):
        with self._lock:
            self.calls.append({
                'endpoint': endpoint,
                'model': body.get('model', ''),
                'auth': request.headers.get('Authorization', ''),
                'body': body,
                'time': time.time(),
            })

    def find(self, endpoint=None, model=None):
        with self._lock:
            calls = list(self.calls)
        return [c for c in calls
                if (endpoint is None or c['endpoint'] == endpoint)
                and (model is None or c['model'] == model)]

    def reset(self):
        with self._lock:
            self.calls = []

    # ---------- 端点 ----------
    def _register(self):
        @self.app.post('/v1/chat/completions')
        def chat():
            body = request.get_json(silent=True) or {}
            self.record('chat', body)
            auth = request.headers.get('Authorization', '')
            model = body.get('model', '')
            if 'k-bad' in auth or model == 'fail401':
                return jsonify({'error': {'message': 'invalid api key'}}), 401
            if model == 'fail500':
                return jsonify({'error': {'message': 'upstream boom'}}), 500
            if model == 'fail400':
                return jsonify({'error': {'message': 'bad request'}}), 400
            if body.get('stream'):
                chunk1 = {'id': 'mock', 'object': 'chat.completion.chunk', 'model': model,
                          'choices': [{'index': 0, 'delta': {'role': 'assistant',
                                                           'content': '流'}}]}
                chunk2 = {'id': 'mock', 'object': 'chat.completion.chunk', 'model': model,
                          'choices': [{'index': 0, 'delta': {'content': '式'}}],
                          'usage': {'prompt_tokens': 3, 'completion_tokens': 2,
                                    'total_tokens': 5}}
                def gen():
                    yield 'data: ' + json.dumps(chunk1, ensure_ascii=False) + '\n\n'
                    yield 'data: ' + json.dumps(chunk2, ensure_ascii=False) + '\n\n'
                    yield 'data: [DONE]\n\n'
                return Response(gen(), mimetype='text/event-stream')
            return jsonify({
                'id': 'mock-1', 'object': 'chat.completion', 'model': model,
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': '来自 ' + model}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            })

        @self.app.post('/v1/images/generations')
        def images():
            body = request.get_json(silent=True) or {}
            self.record('images', body)
            if body.get('model') == 'fail500':
                return jsonify({'error': {'message': 'boom'}}), 500
            return jsonify({'created': int(time.time()),
                            'data': [{'url': 'https://example.com/mock.png'}]})

        @self.app.post('/v1/videos/generations')
        def videos():
            body = request.get_json(silent=True) or {}
            self.record('videos', body)
            return jsonify({'id': 'video_mock_1', 'object': 'video', 'status': 'queued',
                            'model': body.get('model', '')})

        @self.app.get('/v1/models')
        def models():
            auth = request.headers.get('Authorization', '')
            if 'k-bad' in auth:
                return jsonify({'error': {'message': 'invalid api key'}}), 401
            return jsonify({'object': 'list', 'data': [
                {'id': 'gpt-4o-mini', 'object': 'model', 'owned_by': 'openai'},
                {'id': 'text-embedding-3-small', 'object': 'model', 'owned_by': 'openai'},
                {'id': 'dall-e-3', 'object': 'model', 'owned_by': 'openai'},
                {'id': 'sora-1', 'object': 'model', 'owned_by': 'openai'},
                {'id': 'glm-ocr', 'object': 'model', 'owned_by': 'zhipu'},
            ]})


class MockResponse:
    """把 Flask 测试响应包装成 requests.Response 的常用接口。"""

    def __init__(self, resp):
        self.status_code = resp.status_code
        self.content = resp.data
        self.headers = resp.headers

    @property
    def text(self):
        return self.content.decode('utf-8', 'ignore')

    def json(self):
        return json.loads(self.text)

    def close(self):
        pass

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


class MockSession:
    """进程内模拟 requests.Session（测试环境禁止真实 socket）。"""

    def __init__(self, mock):
        self.mock = mock

    def post(self, url, headers=None, data=None, timeout=None, stream=False):
        return self._open('POST', url, headers=headers or {}, data=data)

    def get(self, url, headers=None, timeout=None, params=None):
        return self._open('GET', url, headers=headers or {})

    def _open(self, method, url, **kwargs):
        path = urlsplit(url).path
        client = self.mock.app.test_client()
        return MockResponse(client.open(path, method=method, **kwargs))
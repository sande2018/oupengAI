# -*- coding: utf-8 -*-
"""端到端测试：真实 HTTP 调用假上游，验证路由、故障切换、Key 轮换与 RAG。

运行：
    python -m unittest discover -s tests -t . -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 必须在导入 config 之前指定临时库
TMP_DIR = tempfile.mkdtemp(prefix='oupeng-test-')
os.environ['OUPENG_DATA_DIR'] = TMP_DIR
os.environ['OUPENG_DB_PATH'] = os.path.join(TMP_DIR, 'test.db')
os.environ['OUPENG_RETRY_PER_CANDIDATE'] = '0'

import db
from services import knowledge, router
from app import app
from tests.mock_upstream import MockSession, MockUpstream

MOCK = MockUpstream()
BASE_URL = 'http://mock.local/v1'

_ORIGINAL_HTTP = router._http

TABLES = ['call_logs', 'api_key_kbs', 'api_keys', 'kb_chunks', 'kb_documents',
          'knowledge_bases', 'custom_model_targets', 'custom_models',
          'channel_keys', 'channel_models', 'channels']


def reset_db():
    db.execute('PRAGMA foreign_keys=OFF')
    for table in TABLES:
        db.execute('DELETE FROM %s' % table)
    db.execute('PRAGMA foreign_keys=ON')


def make_channel(name, keys=('k-good',), base_url=BASE_URL):
    now = time.time()
    cid = db.insert('channels', {
        'name': name, 'base_url': base_url, 'enabled': 1, 'sort': 0,
        'timeout': 30, 'video_path': '/videos/generations', 'remark': '',
        'created_at': now, 'updated_at': now})
    for i, key in enumerate(keys):
        db.insert('channel_keys', {
            'channel_id': cid, 'api_key': key, 'label': 'k%d' % i,
            'enabled': 1, 'sort': i, 'created_at': now})
    return cid


def make_model(channel_id, model_name, types, priority=100, weight=100):
    now = time.time()
    return db.insert('channel_models', {
        'channel_id': channel_id, 'model_name': model_name,
        'types': json.dumps(types), 'enabled': 1, 'sort': 0,
        'priority': priority, 'weight': weight, 'timeout': 0, 'remark': '',
        'created_at': now, 'updated_at': now})


def make_custom(name='oupeng', targets=(), strategy='priority'):
    now = time.time()
    row = db.query_one('SELECT id FROM custom_models WHERE name=?', (name,))
    if row:
        cm_id = row['id']
        db.execute('DELETE FROM custom_model_targets WHERE custom_model_id=?', (cm_id,))
    else:
        cm_id = db.insert('custom_models', {
            'name': name, 'display_name': name, 'description': '',
            'strategy': strategy, 'enabled': 1,
            'created_at': now, 'updated_at': now})
    for i, model_id in enumerate(targets):
        db.insert('custom_model_targets', {
            'custom_model_id': cm_id, 'channel_model_id': model_id,
            'enabled': 1, 'priority': 100, 'weight': 100, 'sort': i,
            'created_at': now})
    return cm_id


def make_api_key(kb_ids=()):
    token = 'sk-ou-test-' + uuid.uuid4().hex
    key_id = db.insert('api_keys', {
        'name': 'test', 'key': token, 'prefix': token[:12], 'enabled': 1,
        'rpm_limit': 0, 'notes': '', 'created_at': time.time()})
    for kb_id in kb_ids:
        db.insert('api_key_kbs', {'api_key_id': key_id, 'kb_id': kb_id})
    return token


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 测试环境禁止 socket 连接，用进程内调用替代 requests
        router._http = lambda: MockSession(MOCK)

    @classmethod
    def tearDownClass(cls):
        router._http = _ORIGINAL_HTTP

    def setUp(self):
        reset_db()
        MOCK.reset()
        db.set_setting('rag_min_score', '0.0')
        db.set_setting('rag_top_k', '4')
        self.client = app.test_client()
        self.token = make_api_key()
        self.auth = {'Authorization': 'Bearer ' + self.token}

    # ---------- 工具 ----------
    def chat(self, body=None, headers=None, token=None):
        payload = {'model': 'oupeng',
                   'messages': [{'role': 'user', 'content': '你好'}]}
        payload.update(body or {})
        h = {'Authorization': 'Bearer ' + (token or self.token)}
        h.update(headers or {})
        return self.client.post('/v1/chat/completions', json=payload, headers=h)

    def _text_model(self, name='ok-model', channel='main'):
        cid = make_channel(channel)
        mid = make_model(cid, name, ['text'])
        make_custom('oupeng', [mid])
        return cid, mid

    # ---------- 鉴权 ----------
    def test_auth_required(self):
        resp = self.client.post('/v1/chat/completions', json={
            'model': 'oupeng', 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error']['code'], 'missing_api_key')

    def test_invalid_api_key(self):
        resp = self.chat(token='sk-ou-test-not-exists')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error']['code'], 'invalid_api_key')

    # ---------- 文本路由 ----------
    def test_text_routing(self):
        self._text_model()
        resp = self.chat()
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['model'], 'oupeng')
        self.assertIn('来自 ok-model', data['choices'][0]['message']['content'])
        self.assertEqual(resp.headers['X-Ou-Type'], 'text')
        self.assertEqual(resp.headers['X-Ou-Channel'], 'main')
        calls = MOCK.find('chat', 'ok-model')
        self.assertEqual(len(calls), 1)
        self.assertIn('k-good', calls[0]['auth'])

    def test_unknown_model_404(self):
        resp = self.chat({'model': 'no-such-model'})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['error']['code'], 'model_not_found')

    # ---------- 故障切换 ----------
    def test_failover_to_backup_channel(self):
        c1 = make_channel('primary')
        m1 = make_model(c1, 'fail500', ['text'])
        c2 = make_channel('backup')
        m2 = make_model(c2, 'ok-model', ['text'])
        make_custom('oupeng', [m1, m2])

        resp = self.chat()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers['X-Ou-Channel'], 'backup')
        self.assertIn('来自 ok-model', resp.get_json()['choices'][0]['message']['content'])
        # 主渠道被记录失败次数
        row = db.query_one('SELECT fail_count FROM channels WHERE name=?', ('primary',))
        self.assertEqual(row['fail_count'], 1)

    def test_all_fail_returns_502(self):
        cid = make_channel('only')
        mid = make_model(cid, 'fail500', ['text'])
        make_custom('oupeng', [mid])
        resp = self.chat()
        self.assertEqual(resp.status_code, 502)
        body = resp.get_json()
        self.assertIn('error', body)
        self.assertTrue(body['error']['attempts'])

    # ---------- 多 Key 轮换与冷却 ----------
    def test_key_rotation_and_cooldown(self):
        cid = make_channel('multi', keys=('k-bad', 'k-good'))
        mid = make_model(cid, 'ok-model', ['text'])
        make_custom('oupeng', [mid])

        resp = self.chat()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('来自 ok-model', resp.get_json()['choices'][0]['message']['content'])
        bad = db.query_one("SELECT * FROM channel_keys WHERE api_key='k-bad'")
        good = db.query_one("SELECT * FROM channel_keys WHERE api_key='k-good'")
        self.assertGreater(bad['disabled_until'], time.time())
        self.assertEqual(good['success_count'], 1)
        # 两次尝试：k-bad(401) → k-good(200)
        self.assertGreaterEqual(len(MOCK.find('chat')), 2)

    def test_every_channel_without_keys_still_works(self):
        """免鉴权转中：渠道不配 Key 也能调用。"""
        cid = make_channel('nokey', keys=())
        db.execute('DELETE FROM channel_keys WHERE channel_id=?', (cid,))
        mid = make_model(cid, 'ok-model', ['text'])
        make_custom('oupeng', [mid])
        resp = self.chat()
        self.assertEqual(resp.status_code, 200)

    # ---------- 图片：视觉 vs OCR ----------
    def test_vision_type_selected_for_image(self):
        cv = make_channel('vision-ch')
        mv = make_model(cv, 'vision-model', ['vision'])
        co = make_channel('ocr-ch')
        mo = make_model(co, 'ocr-model', ['ocr'])
        make_custom('oupeng', [mv, mo])

        resp = self.chat({'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '这张图里有什么？'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}},
        ]}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers['X-Ou-Type'], 'vision')
        self.assertEqual(resp.headers['X-Ou-Channel'], 'vision-ch')

    def test_ocr_type_selected_by_prompt(self):
        cv = make_channel('vision-ch')
        mv = make_model(cv, 'vision-model', ['vision'])
        co = make_channel('ocr-ch')
        mo = make_model(co, 'ocr-model', ['ocr'])
        make_custom('oupeng', [mv, mo])

        resp = self.chat({'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '识别图片中的文字'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}},
        ]}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers['X-Ou-Type'], 'ocr')
        self.assertEqual(resp.headers['X-Ou-Channel'], 'ocr-ch')

    def test_ocr_forced_by_header(self):
        cv = make_channel('vision-ch')
        mv = make_model(cv, 'vision-model', ['vision'])
        co = make_channel('ocr-ch')
        mo = make_model(co, 'ocr-model', ['ocr'])
        make_custom('oupeng', [mv, mo])

        resp = self.chat({'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '这张图里有什么'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}},
        ]}]}, headers={'X-Task-Type': 'ocr'})
        self.assertEqual(resp.headers['X-Ou-Type'], 'ocr')
        self.assertEqual(resp.headers['X-Ou-Channel'], 'ocr-ch')

    def test_image_falls_back_to_ocr_model(self):
        """只配了 OCR 模型时，普通看图请求也能兜底到 OCR。"""
        co = make_channel('only-ocr')
        mo = make_model(co, 'ocr-model', ['ocr'])
        make_custom('oupeng', [mo])
        resp = self.chat({'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '这张图里有什么'},
            {'type': 'image_url', 'image_url': {'url': 'http://example.com/a.png'}},
        ]}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers['X-Ou-Type'], 'ocr')

    # ---------- 生图 / 生视频 ----------
    def test_image_generation_routing(self):
        cid = make_channel('gen-ch')
        mid = make_model(cid, 'gen-model', ['gen'])
        make_custom('oupeng', [mid])

        resp = self.client.post('/v1/images/generations', headers=self.auth,
                                json={'model': 'oupeng', 'prompt': '一只猫'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['data'][0]['url'], 'https://example.com/mock.png')
        self.assertEqual(resp.headers['X-Ou-Type'], 'gen')
        calls = MOCK.find('images')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['body']['model'], 'gen-model')
        self.assertEqual(calls[0]['body']['prompt'], '一只猫')

    def test_video_generation_routing(self):
        cid = make_channel('video-ch')
        mid = make_model(cid, 'video-model', ['video_gen'])
        make_custom('oupeng', [mid])

        resp = self.client.post('/v1/videos/generations', headers=self.auth,
                                json={'model': 'oupeng', 'prompt': '一只在雪地里的猫'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['id'], 'video_mock_1')
        self.assertEqual(MOCK.find('videos')[0]['body']['model'], 'video-model')

    # ---------- 流式 ----------
    def test_stream_passthrough(self):
        self._text_model()
        resp = self.chat({'stream': True})
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        self.assertIn('data:', text)
        self.assertIn('流', text)
        self.assertIn('式', text)

    # ---------- 知识库 ----------
    def _make_kb(self):
        now = time.time()
        return db.insert('knowledge_bases', {
            'name': '测试库', 'description': '', 'chunk_size': 200,
            'chunk_overlap': 20, 'top_k': 3, 'min_score': 0.0,
            'enabled': 1, 'created_at': now, 'updated_at': now})

    def test_knowledge_vector_and_search(self):
        kb_id = self._make_kb()
        _, chunks = knowledge.add_document(
            kb_id, '退款政策',
            '退款政策：用户购买后 7 天内可以无理由退款；超过 7 天不支持退款。'
            '退款会在 3 个工作日内原路退回。')
        self.assertGreater(chunks, 0)

        vec = knowledge.embed('退款政策')
        self.assertEqual(len(vec), 512)
        self.assertAlmostEqual(sum(v * v for v in vec), 1.0, places=5)

        hits = knowledge.search([kb_id], '退款要多久', top_k=3, min_score=0.0)
        self.assertTrue(hits)
        self.assertIn('退款', hits[0]['content'])
        self.assertGreater(hits[0]['score'], 0)

    def test_rag_injection_uses_api_key_kb(self):
        kb_id = self._make_kb()
        knowledge.add_document(
            kb_id, '退款政策',
            '退款政策：用户购买后 7 天内可以无理由退款，退款 3 个工作日内原路退回。')
        token = make_api_key([kb_id])
        cid = make_channel('kb-ch')
        mid = make_model(cid, 'ok-model', ['text'])
        make_custom('oupeng', [mid])

        resp = self.chat({'messages': [{'role': 'user', 'content': '退款要等几天？'}]},
                         token=token)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(int(resp.headers['X-Ou-Rag-Hits']), 1)

        body = MOCK.find('chat')[-1]['body']
        self.assertEqual(body['messages'][0]['role'], 'system')
        self.assertIn('退款', body['messages'][0]['content'])
        # 站内扩展字段不能透传给上游
        self.assertNotIn('knowledge', body)

    def test_rag_uses_kb_min_score(self):
        """API 调用时以知识库自己的 min_score 为准，而不是全局设置。"""
        now = time.time()
        kb_id = db.insert('knowledge_bases', {
            'name': '阈值库', 'description': '', 'chunk_size': 200,
            'chunk_overlap': 20, 'top_k': 3, 'min_score': 0.9,
            'enabled': 1, 'created_at': now, 'updated_at': now})
        knowledge.add_document(
            kb_id, '退款政策',
            '退款政策：用户购买后 7 天内可以无理由退款，退款 3 个工作日内原路退回。')
        token = make_api_key([kb_id])
        cid = make_channel('kb-ch')
        mid = make_model(cid, 'ok-model', ['text'])
        make_custom('oupeng', [mid])

        # 知识库阈值很高 → 不注入
        resp = self.chat({'messages': [{'role': 'user', 'content': '退款要几天'}]},
                         token=token)
        self.assertEqual(resp.headers['X-Ou-Rag-Hits'], '0')
        self.assertEqual(MOCK.find('chat')[-1]['body']['messages'][0]['role'], 'user')

        # 即使全局 rag_min_score 很高，只要知识库自身阈值低就应该命中
        db.set_setting('rag_min_score', '0.9')
        db.execute('UPDATE knowledge_bases SET min_score=0 WHERE id=?', (kb_id,))
        resp = self.chat({'messages': [{'role': 'user', 'content': '退款要几天'}]},
                         token=token)
        self.assertGreaterEqual(int(resp.headers['X-Ou-Rag-Hits']), 1)
        body = MOCK.find('chat')[-1]['body']
        self.assertEqual(body['messages'][0]['role'], 'system')
        self.assertIn('退款', body['messages'][0]['content'])

    def test_rag_off_header(self):
        kb_id = self._make_kb()
        knowledge.add_document(kb_id, '退款政策', '退款政策：7 天内可退款。')
        token = make_api_key([kb_id])
        cid = make_channel('kb-ch')
        mid = make_model(cid, 'ok-model', ['text'])
        make_custom('oupeng', [mid])

        resp = self.chat({'messages': [{'role': 'user', 'content': '退款要等几天？'}]},
                         headers={'X-Knowledge': 'off'}, token=token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers['X-Ou-Rag-Hits'], '0')
        body = MOCK.find('chat')[-1]['body']
        self.assertEqual(body['messages'][0]['role'], 'user')

    def test_rag_specific_kb_ids(self):
        kb1 = self._make_kb()
        knowledge.add_document(kb1, 'A 文档', '报销流程：填写报销单后交给财务。')
        kb2 = self._make_kb()
        knowledge.add_document(kb2, 'B 文档', '退款流程：提交订单号后原路退回。')
        token = make_api_key([kb1, kb2])
        cid = make_channel('kb-ch')
        mid = make_model(cid, 'ok-model', ['text'])
        make_custom('oupeng', [mid])

        resp = self.chat({'messages': [{'role': 'user', 'content': '退款怎么退'}],
                          'knowledge': {'kb_ids': [kb2], 'top_k': 2}}, token=token)
        self.assertEqual(resp.status_code, 200)
        body = MOCK.find('chat')[-1]['body']
        self.assertIn('退款流程', body['messages'][0]['content'])
        self.assertNotIn('报销流程', body['messages'][0]['content'])

    # ---------- 其它端点 ----------
    def test_models_endpoint(self):
        self._text_model()
        resp = self.client.get('/v1/models', headers=self.auth)
        self.assertEqual(resp.status_code, 200)
        names = [m['id'] for m in resp.get_json()['data']]
        self.assertIn('oupeng', names)

    def test_embeddings_local(self):
        resp = self.client.post('/v1/embeddings', headers=self.auth,
                                json={'model': 'oupeng', 'input': ['你好', '世界']})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(len(data), 2)
        self.assertEqual(len(data[0]['embedding']), 512)

    def test_usage_recorded_in_log(self):
        self._text_model()
        resp = self.chat()
        self.assertEqual(resp.status_code, 200)
        row = db.query_one('SELECT * FROM call_logs ORDER BY id DESC LIMIT 1')
        self.assertEqual(row['ok'], 1)
        self.assertEqual(row['prompt_tokens'], 10)
        self.assertEqual(row['completion_tokens'], 5)
        self.assertEqual(row['resolved_type'], 'text')


class SchedulerUnitTest(unittest.TestCase):
    """调度顺序/权重轮询/兜底链的纯逻辑测试。"""

    def setUp(self):
        reset_db()
        db.set_setting('rag_min_score', '0.0')

    def test_priority_order(self):
        from services import router
        c1 = make_channel('c1')
        m1 = make_model(c1, 'm1', ['text'])
        c2 = make_channel('c2')
        m2 = make_model(c2, 'm2', ['text'])
        make_custom('oupeng', [m1, m2])
        # 后建的 target 优先级更高时应该排在前面
        db.execute('UPDATE custom_model_targets SET priority=10 WHERE channel_model_id=?', (m2,))
        db.execute('UPDATE custom_model_targets SET priority=20 WHERE channel_model_id=?', (m1,))
        combos = router.build_candidates('text', 'oupeng')
        self.assertEqual([c['model'] for c in combos[:2]], ['m2', 'm1'])

    def test_weighted_spreads_load(self):
        from services import router
        c1 = make_channel('c1')
        m1 = make_model(c1, 'm1', ['text'])
        c2 = make_channel('c2')
        m2 = make_model(c2, 'm2', ['text'])
        make_custom('oupeng', [m1, m2], strategy='weighted')
        first = []
        for _ in range(6):
            combos = router.build_candidates('text', 'oupeng')
            first.append(combos[0]['model'])
        # 两个模型权重相同，前 6 次应该被均分，而不是永远选同一个
        self.assertGreaterEqual(first.count('m1'), 2)
        self.assertGreaterEqual(first.count('m2'), 2)

    def test_split_preserves_ip_url_and_decimal(self):
        """英文句点只有后跟空白才断句，IP/URL/小数/版本号不能被拆碎。"""
        from services import knowledge
        text = ('设备 IP 为 0.0.0.0 时，请检查网络。'
                '下载地址：https://partners.hikvision.com/tools/tooldetail?id=718096962743238656。'
                '价格 19.9 元，版本 v2.10.1。Hello world. How are you?')
        joined = ''.join(knowledge.split_text(text, 500, 50))
        self.assertIn('0.0.0.0', joined)
        self.assertIn('https://partners.hikvision.com/tools/tooldetail?id=718096962743238656',
                      joined)
        self.assertIn('19.9', joined)
        self.assertIn('v2.10.1', joined)
        self.assertIn('Hello world. How are you?', joined)
        self.assertNotIn('0.\n0', joined)

    def test_type_chain(self):
        from services import router
        self.assertEqual(router.type_chain('vision'), ['vision', 'ocr'])
        self.assertEqual(router.type_chain('ocr'), ['ocr', 'vision'])
        self.assertEqual(router.type_chain('text'), ['text', 'vision'])
        self.assertEqual(router.type_chain('gen'), ['gen'])

    def test_key_rotation_start(self):
        from services import router
        cid = make_channel('c', keys=('k1', 'k2', 'k3'))
        mid = make_model(cid, 'm', ['text'])
        make_custom('oupeng', [mid])
        seq = []
        for _ in range(3):
            combos = router.build_candidates('text', 'oupeng')
            seq.append(combos[0]['key'])
        self.assertEqual(sorted(seq), ['k1', 'k2', 'k3'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
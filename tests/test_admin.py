# -*- coding: utf-8 -*-
"""后台页面与 CRUD 冒烟测试。

运行：python -m unittest discover -s tests -t . -v
"""
import json
import os
import sys
import tempfile
import time
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

TMP_DIR = tempfile.mkdtemp(prefix='oupeng-admin-test-')
os.environ.setdefault('OUPENG_DATA_DIR', TMP_DIR)
os.environ.setdefault('OUPENG_DB_PATH', os.path.join(TMP_DIR, 'admin.db'))

from werkzeug.security import generate_password_hash

import db
from app import app
from tests.mock_upstream import MockSession, MockUpstream

MOCK = MockUpstream()


def make_channel(name='渠道', keys=('k-good',), base_url='http://mock.local/v1'):
    now = time.time()
    cid = db.insert('channels', {
        'name': name, 'base_url': base_url, 'enabled': 1, 'sort': 0,
        'timeout': 30, 'video_path': '/videos/generations', 'remark': '',
        'created_at': now, 'updated_at': now})
    for i, key in enumerate(keys):
        db.insert('channel_keys', {
            'channel_id': cid, 'api_key': key, 'label': 'main',
            'enabled': 1, 'sort': i, 'created_at': now})
    return cid

PAGES = ['dashboard', 'channels', 'models', 'knowledge', 'apikeys',
         'logs', 'playground', 'settings']
TABLES = ['call_logs', 'api_key_kbs', 'api_keys', 'kb_chunks', 'kb_documents',
          'knowledge_bases', 'custom_model_targets', 'custom_models',
          'channel_keys', 'channel_models', 'channels']


class AdminTest(unittest.TestCase):
    def setUp(self):
        db.execute('PRAGMA foreign_keys=OFF')
        for table in TABLES:
            db.execute('DELETE FROM %s' % table)
        db.execute('PRAGMA foreign_keys=ON')
        db.set_setting('admin_password_hash', generate_password_hash('admin888'))
        db.set_setting('admin_password_default', '1')
        self.client = app.test_client()

    def login(self):
        resp = self.client.post('/db_admin/api/login', json={'password': 'admin888'})
        self.assertEqual(resp.status_code, 200)
        return resp

    # ---------- 登录与页面 ----------
    def test_admin_requires_login(self):
        resp = self.client.get('/db_admin/dashboard')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/db_admin/login', resp.headers['Location'])
        resp = self.client.get('/db_admin/api/overview')
        self.assertEqual(resp.status_code, 401)

    def test_login_hint_only_for_default_password(self):
        # 仍是默认密码：显示首次启动密码提示
        html = self.client.get('/db_admin/login').get_data(as_text=True)
        self.assertIn('admin888', html)

        # 改成新密码后：不再显示默认密码提示
        self.login()
        resp = self.client.post('/db_admin/api/password', json={
            'old_password': 'admin888', 'new_password': 'newpass123'})
        self.assertEqual(resp.status_code, 200)
        self.client.post('/db_admin/api/logout')

        html = self.client.get('/db_admin/login').get_data(as_text=True)
        self.assertNotIn('admin888', html)
        self.assertIn('请输入管理员密码', html)

    def test_login_page_and_bad_password(self):
        resp = self.client.get('/db_admin/login')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('管理员登录', resp.get_data(as_text=True))
        resp = self.client.post('/db_admin/api/login', json={'password': 'wrong'})
        self.assertEqual(resp.status_code, 401)

    def test_all_pages_render(self):
        self.login()
        for page in PAGES:
            resp = self.client.get('/db_admin/' + page)
            self.assertEqual(resp.status_code, 200, '页面 %s 渲染失败' % page)
            html = resp.get_data(as_text=True)
            self.assertIn('欧朋AI', html)
            self.assertIn('/db_admin/api/', html)      # 页面脚本引用了后台 API

    # ---------- 完整配置流程 ----------
    def test_full_crud_flow(self):
        self.login()

        # 1) 渠道 + 多 Key
        resp = self.client.post('/db_admin/api/channels', json={
            'name': '主渠道', 'base_url': 'http://127.0.0.1:1/v1',
            'timeout': 60, 'enabled': 1,
            'keys': [{'api_key': 'sk-alpha-1234567890', 'label': 'A'},
                     {'api_key': 'sk-beta-0987654321', 'label': 'B'}],
        })
        self.assertEqual(resp.status_code, 201)
        cid = resp.get_json()['id']

        resp = self.client.get('/db_admin/api/channels')
        ch = resp.get_json()['data'][0]
        self.assertEqual(ch['key_count'], 2)
        self.assertNotIn('sk-alpha', str(ch))       # 列表不回传明文
        self.assertIn('…', ch['keys'][0]['masked'])

        # 2) 渠道模型
        resp = self.client.post('/db_admin/api/models', json={
            'channel_id': cid, 'model_name': 'gpt-4o-mini',
            'types': ['text', 'vision'], 'priority': 10, 'weight': 100,
        })
        self.assertEqual(resp.status_code, 201)
        mid = resp.get_json()['id']
        resp = self.client.post('/db_admin/api/models', json={
            'channel_id': cid, 'model_name': 'bad', 'types': []})
        self.assertEqual(resp.status_code, 400)

        # 3) 对外模型（调度组）
        resp = self.client.post('/db_admin/api/custom-models', json={
            'name': 'oupeng-test', 'display_name': '测试模型',
            'strategy': 'weighted',
            'targets': [{'channel_model_id': mid, 'priority': 100, 'weight': 200}],
        })
        self.assertEqual(resp.status_code, 201)
        cm_id = resp.get_json()['id']
        resp = self.client.get('/db_admin/api/custom-models')
        cm = [x for x in resp.get_json()['data'] if x['id'] == cm_id][0]
        self.assertEqual(cm['name'], 'oupeng-test')
        self.assertEqual(cm['targets'][0]['types'], ['text', 'vision'])

        # 4) 知识库 + 文档 + 检索
        resp = self.client.post('/db_admin/api/kbs', json={
            'name': 'FAQ', 'chunk_size': 200, 'chunk_overlap': 20, 'top_k': 3})
        self.assertEqual(resp.status_code, 201)
        kb_id = resp.get_json()['id']
        resp = self.client.post('/db_admin/api/kbs/%d/documents' % kb_id, json={
            'title': '退款政策',
            'content': '退款政策：购买后 7 天内可以无理由退款。退款 3 个工作日内原路退回。'})
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(resp.get_json()['chunks'], 0)

        resp = self.client.post('/db_admin/api/kbs/%d/search' % kb_id, json={'query': '退款'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['data'])

        resp = self.client.get('/db_admin/api/kbs')
        kb = resp.get_json()['data'][0]
        self.assertEqual(kb['doc_count'], 1)
        self.assertGreater(kb['chunk_count'], 0)

        # 5) 站内 API Key 绑定知识库
        resp = self.client.post('/db_admin/api/api-keys', json={
            'name': '小程序', 'kb_ids': [kb_id], 'rpm_limit': 60})
        self.assertEqual(resp.status_code, 201)
        token = resp.get_json()['key']
        self.assertTrue(token.startswith('sk-ou-'))
        key_id = resp.get_json()['id']

        resp = self.client.get('/db_admin/api/api-keys')
        row = [x for x in resp.get_json()['data'] if x['id'] == key_id][0]
        self.assertEqual(row['kb_ids'], [kb_id])

        # 6) 用这个 Key 调兼容接口（无可用上游 → 503/502，但鉴权与 RAG 流程已走通）
        resp = self.client.post('/v1/chat/completions', headers={
            'Authorization': 'Bearer ' + token},
            json={'model': 'oupeng-test', 'messages': [{'role': 'user', 'content': '退款多久'}]})
        self.assertIn(resp.status_code, (502, 503))

        # 7) 概览
        resp = self.client.get('/db_admin/api/overview')
        data = resp.get_json()
        self.assertEqual(data['counts']['channels'], 1)
        self.assertEqual(data['counts']['models'], 1)
        self.assertEqual(data['counts']['kbs'], 1)

        # 8) 日志
        resp = self.client.get('/db_admin/api/logs?page=1&size=10')
        self.assertEqual(resp.status_code, 200)

        # 9) 删除
        resp = self.client.delete('/db_admin/api/custom-models/%d' % cm_id)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.delete('/db_admin/api/channels/%d' % cid)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get('/db_admin/api/models')
        self.assertEqual(len(resp.get_json()['data']), 0)

    def test_update_document(self):
        self.login()
        resp = self.client.post('/db_admin/api/kbs', json={
            'name': '编辑测试', 'chunk_size': 200, 'chunk_overlap': 20,
            'min_score': 0.0})
        self.assertEqual(resp.status_code, 201)
        kb_id = resp.get_json()['id']

        resp = self.client.post('/db_admin/api/kbs/%d/documents' % kb_id, json={
            'title': '旧标题', 'content': '退款政策：购买后 7 天内可以无理由退款。'})
        self.assertEqual(resp.status_code, 200)
        doc_id = resp.get_json()['doc_id']

        resp = self.client.put('/db_admin/api/documents/%d' % doc_id, json={
            'title': '发票说明',
            'content': '发票说明：下单后可在订单详情页申请电子发票，3 个工作日内开具。'})
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(resp.get_json()['chunks'], 0)

        doc = self.client.get('/db_admin/api/documents/%d' % doc_id).get_json()['data']
        self.assertEqual(doc['title'], '发票说明')
        self.assertIn('发票', doc['content'])
        self.assertGreater(doc['chunk_count'], 0)

        # 新内容可检索，旧内容不再命中
        resp = self.client.post('/db_admin/api/kbs/%d/search' % kb_id, json={
            'query': '电子发票', 'min_score': 0.01})
        hits = resp.get_json()['data']
        self.assertTrue(hits)
        self.assertIn('发票', hits[0]['content'])

        resp = self.client.post('/db_admin/api/kbs/%d/search' % kb_id, json={
            'query': '退款', 'min_score': 0.01})
        self.assertFalse([h for h in resp.get_json()['data'] if '退款' in h['content']])

        # 空内容 / 不存在的文档
        resp = self.client.put('/db_admin/api/documents/%d' % doc_id, json={'content': '  '})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            self.client.put('/db_admin/api/documents/999999',
                            json={'content': 'x'}).status_code, 404)

    def test_polish_document(self):
        from services import router
        old_http = router._http
        router._http = lambda: MockSession(MOCK)
        try:
            self.login()
            cid = make_channel('润色渠道')
            resp = self.client.post('/db_admin/api/models', json={
                'channel_id': cid, 'model_name': 'ok-model', 'types': ['text']})
            mid = resp.get_json()['id']
            resp = self.client.post('/db_admin/api/custom-models', json={
                'name': 'oupeng-polish',
                'targets': [{'channel_model_id': mid}]})
            self.assertEqual(resp.status_code, 201)

            resp = self.client.post('/db_admin/api/ai/polish-document', json={
                'model': 'oupeng-polish', 'title': '退款说明',
                'content': '我们这边七天之内都能退，超过就不行了。'})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data['content'])
            self.assertEqual(data['channel'], '润色渠道')
            self.assertEqual(data['model'], 'ok-model')

            # 不指定模型 → 自动走文本模型
            resp = self.client.post('/db_admin/api/ai/polish-document',
                                    json={'content': '原始文档'})
            self.assertEqual(resp.status_code, 200)

            # 空内容 / 超长 / 未知模型
            self.assertEqual(self.client.post('/db_admin/api/ai/polish-document',
                                              json={'content': ''}).status_code, 400)
            self.assertEqual(self.client.post('/db_admin/api/ai/polish-document',
                                              json={'content': 'x' * 60001}).status_code, 400)
            self.assertEqual(self.client.post('/db_admin/api/ai/polish-document',
                                              json={'model': 'no-such',
                                                    'content': 'x'}).status_code, 400)
        finally:
            router._http = old_http

    def test_polish_document_batches(self):
        """长文档应自动分批调用模型，并把各批结果拼起来。"""
        from services import router
        old_http = router._http
        router._http = lambda: MockSession(MOCK)
        try:
            self.login()
            cid = make_channel('分批渠道')
            resp = self.client.post('/db_admin/api/models', json={
                'channel_id': cid, 'model_name': 'ok-model', 'types': ['text']})
            mid = resp.get_json()['id']
            resp = self.client.post('/db_admin/api/custom-models', json={
                'name': 'oupeng-batch',
                'targets': [{'channel_model_id': mid}]})
            self.assertEqual(resp.status_code, 201)

            content = '退款政策：用户购买后 7 天内可以无理由退款。' * 400
            resp = self.client.post('/db_admin/api/ai/polish-document', json={
                'model': 'oupeng-batch', 'title': '长文档', 'content': content})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertGreaterEqual(data['batches'], 2)
            self.assertEqual(data['content'].count('来自 ok-model'), data['batches'])
        finally:
            router._http = old_http

    def test_settings_and_password(self):
        self.login()
        resp = self.client.put('/db_admin/api/settings', json={
            'rag_top_k': 6, 'rag_min_score': 0.2, 'rag_max_chars': 4000})
        self.assertEqual(resp.status_code, 200)
        data = self.client.get('/db_admin/api/settings').get_json()['data']
        self.assertEqual(data['rag_top_k'], '6')

        resp = self.client.post('/db_admin/api/password', json={
            'old_password': 'admin888', 'new_password': 'newpass123'})
        self.assertEqual(resp.status_code, 200)
        self.client.post('/db_admin/api/logout')
        resp = self.client.post('/db_admin/api/login', json={'password': 'admin888'})
        self.assertEqual(resp.status_code, 401)
        resp = self.client.post('/db_admin/api/login', json={'password': 'newpass123'})
        self.assertEqual(resp.status_code, 200)
        # 改回来，避免影响其它测试
        db.set_setting('admin_password_hash', generate_password_hash('admin888'))
        db.set_setting('admin_password_default', '1')

    def test_types_endpoint(self):
        self.login()
        resp = self.client.get('/db_admin/api/ai-types')
        self.assertEqual(resp.status_code, 200)
        keys = [x['key'] for x in resp.get_json()['data']]
        self.assertEqual(keys, ['text', 'vision', 'ocr', 'gen', 'video_gen', 'video_vision'])

    # ---------- 从上游获取模型 ----------
    def test_toggle_model_and_channel(self):
        self.login()
        cid = make_channel('切换渠道')
        resp = self.client.post('/db_admin/api/models', json={
            'channel_id': cid, 'model_name': 'toggle-model', 'types': ['text']})
        self.assertEqual(resp.status_code, 201)
        mid = resp.get_json()['id']

        resp = self.client.post('/db_admin/api/models/%d/toggle' % mid)
        self.assertEqual(resp.get_json()['enabled'], 0)
        self.assertEqual(db.query_one('SELECT enabled FROM channel_models WHERE id=?',
                                      (mid,))['enabled'], 0)

        resp = self.client.post('/db_admin/api/models/%d/toggle' % mid)
        self.assertEqual(resp.get_json()['enabled'], 1)

        resp = self.client.post('/db_admin/api/channels/%d/toggle' % cid)
        self.assertEqual(resp.get_json()['enabled'], 0)
        self.assertEqual(db.query_one('SELECT enabled FROM channels WHERE id=?',
                                      (cid,))['enabled'], 0)

        resp = self.client.post('/db_admin/api/channels/%d/toggle' % cid)
        self.assertEqual(resp.get_json()['enabled'], 1)

        self.assertEqual(self.client.post('/db_admin/api/models/999999/toggle').status_code, 404)
        self.assertEqual(self.client.post('/db_admin/api/channels/999999/toggle').status_code, 404)

    def test_apikeys_page_has_base_url(self):
        self.login()
        html = self.client.get('/db_admin/apikeys').get_data(as_text=True)
        self.assertIn('id="base-url"', html)
        self.assertIn('http://localhost/v1', html)
        self.assertIn('copyBaseUrl()', html)

    def test_remote_models_fetch_and_import(self):
        from services import router
        old_http = router._http
        router._http = lambda: MockSession(MOCK)
        try:
            self.login()
            cid = make_channel('拉模型渠道')

            resp = self.client.get('/db_admin/api/channels/%d/remote-models' % cid)
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body['count'], 5)
            self.assertEqual(body['key_label'], 'main')
            by_id = {m['id']: m for m in body['data']}
            self.assertEqual(by_id['gpt-4o-mini']['suggested_types'], ['text', 'vision'])
            self.assertEqual(by_id['dall-e-3']['suggested_types'], ['gen'])
            self.assertEqual(by_id['sora-1']['suggested_types'], ['video_gen'])
            self.assertEqual(by_id['glm-ocr']['suggested_types'], ['ocr'])
            self.assertEqual(by_id['text-embedding-3-small']['suggested_types'], [])
            self.assertFalse(by_id['gpt-4o-mini']['exists'])

            resp = self.client.post(
                '/db_admin/api/channels/%d/remote-models/import' % cid, json={
                    'models': [
                        {'model_name': 'gpt-4o-mini', 'types': ['text', 'vision']},
                        {'model_name': 'dall-e-3', 'types': ['gen']},
                        {'model_name': 'glm-ocr', 'types': ['ocr']},
                        {'model_name': 'no-type', 'types': []},
                    ]})
            self.assertEqual(resp.status_code, 200)
            result = resp.get_json()
            self.assertEqual(result['created'], 3)
            self.assertEqual(result['skipped'], 1)

            # 已导入的模型应标记 exists，并沿用本地类型
            resp = self.client.get('/db_admin/api/channels/%d/remote-models' % cid)
            by_id = {m['id']: m for m in resp.get_json()['data']}
            self.assertTrue(by_id['gpt-4o-mini']['exists'])
            self.assertEqual(by_id['gpt-4o-mini']['suggested_types'], ['text', 'vision'])
            self.assertFalse(by_id['text-embedding-3-small']['exists'])

            # 再次导入同一模型 = 更新类型
            resp = self.client.post(
                '/db_admin/api/channels/%d/remote-models/import' % cid, json={
                    'models': [{'model_name': 'gpt-4o-mini', 'types': ['text']}]})
            self.assertEqual(resp.get_json()['updated'], 1)

            resp = self.client.get('/db_admin/api/models?channel_id=%d' % cid)
            models = resp.get_json()['data']
            self.assertEqual(len(models), 3)
            gpt = [m for m in models if m['model_name'] == 'gpt-4o-mini'][0]
            self.assertEqual(gpt['types'], ['text'])
        finally:
            router._http = old_http

    def test_test_model_by_name(self):
        from services import router
        old_http = router._http
        router._http = lambda: MockSession(MOCK)
        try:
            self.login()
            cid = make_channel('测试渠道')

            resp = self.client.post('/db_admin/api/channels/%d/test-model' % cid, json={
                'model_name': 'ok-model', 'types': ['text', 'vision']})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()['data']
            self.assertEqual([x['type'] for x in data], ['text', 'vision'])
            self.assertTrue(all(x['ok'] for x in data))
            self.assertIn('来自 ok-model', data[0]['reply'])

            resp = self.client.post('/db_admin/api/channels/%d/test-model' % cid, json={
                'model_name': 'fail400', 'types': ['text']})
            item = resp.get_json()['data'][0]
            self.assertFalse(item['ok'])
            self.assertIn('400', item['error'])
            self.assertIn('fail400', item['error'])

            # 视频识别默认使用内置公开 URL，并应把 video_url 传给上游
            resp = self.client.post('/db_admin/api/channels/%d/test-model' % cid, json={
                'model_name': 'ok-model', 'types': ['video_vision']})
            self.assertTrue(resp.get_json()['data'][0]['ok'])
            content = MOCK.find('chat', 'ok-model')[-1]['body']['messages'][0]['content']
            self.assertTrue(any(p.get('type') == 'video_url' for p in content))

            # 生图 / 生视频
            resp = self.client.post('/db_admin/api/channels/%d/test-model' % cid, json={
                'model_name': 'gen-model', 'types': ['gen']})
            self.assertTrue(resp.get_json()['data'][0]['ok'])
            self.assertIn('mock.png', resp.get_json()['data'][0]['reply'])

            resp = self.client.post('/db_admin/api/channels/%d/test-model' % cid, json={
                'model_name': 'video-model', 'types': ['video_gen']})
            self.assertTrue(resp.get_json()['data'][0]['ok'])

            # 未选类型 → 400
            resp = self.client.post('/db_admin/api/channels/%d/test-model' % cid, json={
                'model_name': 'ok-model', 'types': []})
            self.assertEqual(resp.status_code, 400)

            # 测试失败不能冷却 Key（不能影响正常调度）
            cid2 = make_channel('坏 Key 渠道', keys=('k-bad',))
            resp = self.client.post('/db_admin/api/channels/%d/test-model' % cid2, json={
                'model_name': 'ok-model', 'types': ['text']})
            self.assertFalse(resp.get_json()['data'][0]['ok'])
            row = db.query_one('SELECT disabled_until FROM channel_keys WHERE channel_id=?',
                               (cid2,))
            self.assertEqual(row['disabled_until'], 0)
        finally:
            router._http = old_http

    def test_remote_models_disable_and_reenable(self):
        from services import router
        old_http = router._http
        router._http = lambda: MockSession(MOCK)
        try:
            self.login()
            cid = make_channel('启停测试')
            resp = self.client.post(
                '/db_admin/api/channels/%d/remote-models/import' % cid, json={
                    'models': [
                        {'model_name': 'gpt-4o-mini', 'types': ['text', 'vision']},
                        {'model_name': 'dall-e-3', 'types': ['gen']},
                    ]})
            self.assertEqual(resp.get_json()['created'], 2)

            # 取消勾选已存在模型 → 只传 enabled:0，不带类型
            resp = self.client.post(
                '/db_admin/api/channels/%d/remote-models/import' % cid, json={
                    'models': [{'model_name': 'gpt-4o-mini', 'enabled': 0}]})
            result = resp.get_json()
            self.assertEqual(result['disabled'], 1)
            self.assertEqual(result['created'], 0)
            row = db.query_one(
                "SELECT enabled, types FROM channel_models WHERE channel_id=? "
                "AND model_name='gpt-4o-mini'", (cid,))
            self.assertEqual(row['enabled'], 0)
            self.assertEqual(json.loads(row['types']), ['text', 'vision'])  # 类型保留

            # 拉取时返回 enabled=False，另一个模型仍为启用
            resp = self.client.get('/db_admin/api/channels/%d/remote-models' % cid)
            by_id = {m['id']: m for m in resp.get_json()['data']}
            self.assertTrue(by_id['gpt-4o-mini']['exists'])
            self.assertFalse(by_id['gpt-4o-mini']['enabled'])
            self.assertTrue(by_id['dall-e-3']['enabled'])

            # 重新勾选并提交类型 → 恢复启用
            resp = self.client.post(
                '/db_admin/api/channels/%d/remote-models/import' % cid, json={
                    'models': [{'model_name': 'gpt-4o-mini', 'types': ['text'],
                                'enabled': 1}]})
            self.assertEqual(resp.get_json()['updated'], 1)
            row = db.query_one(
                "SELECT enabled, types FROM channel_models WHERE channel_id=? "
                "AND model_name='gpt-4o-mini'", (cid,))
            self.assertEqual(row['enabled'], 1)
            self.assertEqual(json.loads(row['types']), ['text'])

            # 未导入的新模型 + 无类型 → 不创建，计入跳过
            resp = self.client.post(
                '/db_admin/api/channels/%d/remote-models/import' % cid, json={
                    'models': [{'model_name': 'brand-new', 'enabled': 0}]})
            self.assertEqual(resp.get_json()['skipped'], 1)
            self.assertIsNone(db.query_one(
                "SELECT id FROM channel_models WHERE channel_id=? AND model_name='brand-new'",
                (cid,)))
        finally:
            router._http = old_http

    def test_remote_models_bad_key(self):
        from services import router
        old_http = router._http
        router._http = lambda: MockSession(MOCK)
        try:
            self.login()
            cid = make_channel('坏 Key 渠道', keys=('k-bad',))
            resp = self.client.get('/db_admin/api/channels/%d/remote-models' % cid)
            self.assertEqual(resp.status_code, 502)
            self.assertIn('401', resp.get_json()['error'])
        finally:
            router._http = old_http


if __name__ == '__main__':
    unittest.main(verbosity=2)
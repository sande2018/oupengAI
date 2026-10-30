# -*- coding: utf-8 -*-
"""欧朋AI 应用入口。

启动：python app.py
默认地址：http://127.0.0.1:8000/db_admin/
"""
import os
import time
from datetime import timedelta

from flask import Flask, redirect, request

from config import (DATA_DIR, DEBUG, HOST, MAX_REQUEST_BYTES, PORT,
                    SECRET_KEY, UPLOAD_DIR)
from db import init_db
from routes.admin_api import bp as admin_api_bp
from routes.admin_pages import bp as admin_pages_bp
from routes.openai_api import bp as openai_bp


def create_app():
    app = Flask(__name__, static_folder='static', template_folder='templates')
    app.config.update(
        SECRET_KEY=SECRET_KEY,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
    )
    app.json.ensure_ascii = False

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    init_db()

    app.register_blueprint(openai_bp)
    app.register_blueprint(admin_api_bp)
    app.register_blueprint(admin_pages_bp)

    @app.before_request
    def _cors_preflight():
        if request.method == 'OPTIONS' and request.path.startswith('/v1/'):
            return app.make_response(('', 204))

    @app.after_request
    def _cors_headers(resp):
        # 仅对公开的 OpenAI 兼容端点开放跨域（后台接口依赖同源 Cookie，不开 CORS）
        if request.path.startswith('/v1/'):
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin') or '*'
            resp.headers['Access-Control-Allow-Headers'] = (
                'Authorization, Content-Type, X-Task-Type, X-Api-Key, '
                'X-Knowledge, X-Knowledge-Ids')
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            resp.headers['Access-Control-Expose-Headers'] = (
                'X-Ou-Model, X-Ou-Channel, X-Ou-Type, X-Ou-Rag-Hits, X-Ou-Latency-Ms')
        return resp

    @app.route('/')
    def _index():
        return redirect('/db_admin/dashboard')

    @app.template_filter('dt')
    def _dt(value):
        try:
            return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(value or 0)))
        except (TypeError, ValueError, OSError):
            return '-'

    @app.errorhandler(404)
    def _not_found(_e):
        if request.path.startswith('/v1/'):
            return {'error': {'message': '接口不存在：%s' % request.path,
                              'type': 'invalid_request_error', 'code': 'not_found'}}, 404
        return '页面不存在', 404

    @app.errorhandler(413)
    def _too_large(_e):
        return {'error': {'message': '请求体过大', 'type': 'invalid_request_error',
                          'code': 413}}, 413

    return app


app = create_app()

if __name__ == '__main__':
    print('欧朋AI 已启动： http://127.0.0.1:%d/db_admin/' % PORT)
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True)
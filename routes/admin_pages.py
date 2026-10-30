# -*- coding: utf-8 -*-
"""后台页面路由（只负责渲染模板，数据由 /db_admin/api/* 提供）。"""
from flask import Blueprint, redirect, render_template, request, session
from werkzeug.security import check_password_hash

from config import ADMIN_DEFAULT_PASSWORD
from db import get_setting

bp = Blueprint('admin_pages', __name__, url_prefix='/db_admin')


def _using_default_password():
    """当前管理员密码是否仍等于首次启动的默认密码。"""
    stored = get_setting('admin_password_hash')
    if not stored:
        return False
    try:
        return check_password_hash(stored, ADMIN_DEFAULT_PASSWORD)
    except Exception:
        return False

PAGES = {
    'dashboard': ('概览', 'dashboard.html'),
    'channels': ('渠道与 Key', 'channels.html'),
    'models': ('智能调度', 'models.html'),
    'knowledge': ('知识库', 'knowledge.html'),
    'apikeys': ('API Key', 'apikeys.html'),
    'logs': ('调用日志', 'logs.html'),
    'playground': ('在线测试', 'playground.html'),
    'settings': ('系统设置', 'settings.html'),
}


@bp.before_request
def _guard():
    if request.endpoint == 'admin_pages.login':
        return None
    if not session.get('admin'):
        return redirect('/db_admin/login')
    return None


@bp.route('/')
def index():
    return redirect('/db_admin/dashboard')


@bp.route('/login')
def login():
    if session.get('admin'):
        return redirect('/db_admin/dashboard')
    return render_template('login.html',
                           show_default_hint=_using_default_password(),
                           default_password=ADMIN_DEFAULT_PASSWORD)


@bp.route('/<page>')
def page(page):
    if page not in PAGES:
        return redirect('/db_admin/dashboard')
    title, template = PAGES[page]
    return render_template(template, active=page, page_title=title)
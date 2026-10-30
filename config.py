# -*- coding: utf-8 -*-
"""欧朋AI 全局配置。

设计参考 ai.py：渠道可以有多组 API Key、多个模型，模型上挂「能力类型」，
调用时按「渠道 → 模型 → Key」展开组合逐个尝试。

所有可调项都支持环境变量覆盖，方便部署与自动化测试。
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- 目录与文件 ----------
DATA_DIR = os.environ.get('OUPENG_DATA_DIR') or os.path.join(BASE_DIR, 'data')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
DB_PATH = os.environ.get('OUPENG_DB_PATH') or os.path.join(DATA_DIR, 'oupeng.db')

# ---------- Flask ----------
SECRET_KEY = os.environ.get('OUPENG_SECRET_KEY') or 'oupeng-ai-dev-secret-please-change'
HOST = os.environ.get('OUPENG_HOST') or '0.0.0.0'
PORT = int(os.environ.get('OUPENG_PORT') or 8000)
DEBUG = (os.environ.get('OUPENG_DEBUG') or '').strip() in ('1', 'true', 'yes')

# 首次启动时创建的管理员密码（登录后请在后台修改）
ADMIN_DEFAULT_PASSWORD = os.environ.get('OUPENG_ADMIN_PASSWORD') or 'admin888'

# ---------- 上游调用 ----------
CONNECT_TIMEOUT = float(os.environ.get('OUPENG_CONNECT_TIMEOUT') or 10)
DEFAULT_UPSTREAM_TIMEOUT = int(os.environ.get('OUPENG_UPSTREAM_TIMEOUT') or 180)
MAX_REQUEST_BYTES = int(os.environ.get('OUPENG_MAX_REQUEST_BYTES') or 64 * 1024 * 1024)

# 失败重试策略（单个候选组合内部的重试次数，跨候选切换不占用该预算）
RETRY_PER_CANDIDATE = int(os.environ.get('OUPENG_RETRY_PER_CANDIDATE') or 0)
# 判定为「客户端错误」的状态码：换 Key 也没用，直接跳过该候选
CLIENT_ERROR_CODES = (400, 404, 413, 422)
# 判定为「Key 失效/受限」的状态码：冷却该 Key 后继续尝试同渠道其它 Key
KEY_COOLDOWN_CODES = (401, 403, 429)
KEY_COOLDOWN_SECONDS = 300
KEY_RATE_LIMIT_COOLDOWN = 60
# 渠道熔断：连续失败次数与冷却时长
CHANNEL_FAIL_THRESHOLD = 5
CHANNEL_COOLDOWN_SECONDS = 60

# ---------- 知识库 ----------
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 80
DEFAULT_TOP_K = 4
DEFAULT_MIN_SCORE = 0.05
VECTOR_DIM = 512            # 本地哈希向量维度
MAX_CHUNKS_PER_DOC = 4000
RAG_POLISH_MAX_CHARS = 60000      # 单次润色的总字数上限
RAG_POLISH_BATCH_CHARS = 6000     # 超过这个字数自动分批，每批约 6000 字
RAG_POLISH_MAX_BATCHES = 12       # 最多批数（防止一次请求拖太久）

# ---------- 能力类型（后台勾选 + 自动路由的唯一来源） ----------
# key 同时用于：数据库里模型 types 字段、自定义模型 targets、请求头 X-Task-Type。
AI_TYPES = {
    'text': {
        'label': '文本',
        'desc': '纯文本对话 / 写作 / 总结，走 chat/completions',
        'endpoint': '/chat/completions',
        'icon': 'T',
    },
    'vision': {
        'label': '图片理解',
        'desc': '看懂图片内容（描述、问答、推理），走多模态 chat/completions',
        'endpoint': '/chat/completions',
        'icon': 'V',
    },
    'ocr': {
        'label': '图片识别',
        'desc': '专做识别的 OCR 模型：图片转文字、票据/菜单/证件识别',
        'endpoint': '/chat/completions',
        'icon': 'O',
    },
    'gen': {
        'label': '生图',
        'desc': '文生图，走 images/generations',
        'endpoint': '/images/generations',
        'icon': 'G',
    },
    'video_gen': {
        'label': '生视频',
        'desc': '文生视频 / 图生视频，走 videos/generations',
        'endpoint': '/videos/generations',
        'icon': 'M',
    },
    'video_vision': {
        'label': '视频识别',
        'desc': '看懂视频内容（抽帧理解、摘要、问答），走多模态 chat/completions',
        'endpoint': '/chat/completions',
        'icon': 'F',
    },
}
AI_TYPE_ORDER = ('text', 'vision', 'ocr', 'gen', 'video_gen', 'video_vision')

# 需要走 chat/completions 的类型
CHAT_TYPES = ('text', 'vision', 'ocr', 'video_vision')
# 含图片请求的兜底顺序：图片理解优先，OCR 模型作为兜底（OCR 模型通常也能描述图片）
IMAGE_TYPE_CHAIN = ('vision', 'ocr')
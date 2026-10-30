# -*- coding: utf-8 -*-
"""六类能力的连通性测试案例（后台「测试」按钮使用）。

每个案例都是最小可用请求，尽量不使用可选参数（size / response_format / duration
等），避免某些模型因为不支持这些字段直接报 400：

  - text          一句短问题，要求只回复两个字
  - vision        一张红色方块图，问颜色
  - ocr           一张写有 “OUPENG 2026” 的白底黑字图，要求识别文字
  - video_vision  需要视频 URL（默认用一个公开示例，可在后台覆盖）
  - gen           文生图，只传 prompt / n
  - video_gen     文生视频，只传 prompt / n
"""
from services.testcases_assets import OCR_IMAGE_B64, VISION_IMAGE_B64

# 公开示例视频（10 秒 Big Buck Bunny），可在测试弹窗里替换成自己的视频
DEFAULT_VIDEO_URL = 'https://www.w3schools.com/html/mov_bbb.mp4'


def image_data_url(b64):
    return 'data:image/png;base64,' + b64


def _chat_payload(text, image_b64=None, video_url=None, max_tokens=64):
    content = [{'type': 'text', 'text': text}]
    if image_b64:
        content.append({'type': 'image_url', 'image_url': {'url': image_data_url(image_b64)}})
    if video_url:
        content.append({'type': 'video_url', 'video_url': {'url': video_url}})
    return {
        'messages': [{'role': 'user', 'content': content}],
        'temperature': 0,
        'max_tokens': max_tokens,
    }


def build(mtype, video_url=''):
    """返回 (endpoint, payload)。endpoint 为 None 时使用渠道配置的生视频路径。

    参数不合法时抛 ValueError，由调用方转成「未通过」结果。
    """
    if mtype == 'text':
        return '/chat/completions', _chat_payload('你好，请只回复两个字：收到')
    if mtype == 'vision':
        return '/chat/completions', _chat_payload(
            '这张图片是什么颜色？请只回答颜色，不要解释。', VISION_IMAGE_B64)
    if mtype == 'ocr':
        return '/chat/completions', _chat_payload(
            '请识别图片中的文字，只输出文字本身，不要解释。', OCR_IMAGE_B64)
    if mtype == 'video_vision':
        url = (video_url or DEFAULT_VIDEO_URL).strip()
        if not url:
            raise ValueError('视频识别测试需要一个可访问的视频 URL')
        return '/chat/completions', _chat_payload(
            '请用一句话描述这个视频的内容。', video_url=url)
    if mtype == 'gen':
        return '/images/generations', {
            'prompt': 'a red apple on a white table, product photo',
            'n': 1,
        }
    if mtype == 'video_gen':
        return None, {'prompt': 'a cat walking in the snow', 'n': 1}
    raise ValueError('未知的能力类型：%s' % mtype)
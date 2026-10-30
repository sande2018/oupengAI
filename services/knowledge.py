# -*- coding: utf-8 -*-
"""知识库：本地哈希向量（零外部依赖）+ 切片 + 余弦检索 + RAG 注入。

向量方案（完全离线，不需要 embedding 接口）：
  1. 文本切成三种 token：中文单字、中文二元组（bigram，语义主力）、英文/数字词；
  2. 用 hashing trick 把 token 映射到固定的 512 维空间（md5 取前 4 字节 + 符号位），
     避免 Python 内置 hash 的进程随机化；
  3. 词频用 1+log(tf) 压制长文，再整体做 L2 归一化，于是余弦相似度 = 点积；
  4. 向量以 float32 小端 BLOB 存 SQLite，检索时全表扫描（万级切片仍在百毫秒量级）。
"""
import hashlib
import io
import math
import os
import re
import struct

from config import (
    VECTOR_DIM, DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP,
    DEFAULT_TOP_K, DEFAULT_MIN_SCORE, MAX_CHUNKS_PER_DOC,
)
from db import query_all, query_one, execute, get_conn, ts

_EN_WORD = re.compile(r'[a-zA-Z0-9_\-\+.]+')
_CN_CHAR = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
_CN_SEG = re.compile(r'[^\u4dbf\u3400-\u9fff]+')
# 句子断点：中文句末标点、换行，以及「英文标点 + 空白」。
# 关键：英文句点只有后面跟空白时才算句末，避免把 0.0.0.0 / 域名 / 小数 / 版本号切碎。
_BREAK = re.compile(r'[。！？；]+|\n+|[!?]+(?=\s)|\.(?=\s)')


# ---------- 向量化 ----------
def _tokens(text):
    """产出 (token, 权重)。中文 bigram 权重最高，单字兜底，英文词次之。

    中文按「连续汉字段」切分后再取 bigram，避免把「。退款」跨标点拼成无意义二元组。
    """
    text = (text or '').lower()
    for word in _EN_WORD.findall(text):
        if len(word) >= 2:
            yield 'w:' + word, 1.6
    for seg in _CN_SEG.split(text):
        if not seg:
            continue
        for ch in seg:
            yield 'u:' + ch, 0.35
        for i in range(len(seg) - 1):
            yield 'b:' + seg[i:i + 2], 1.0


def embed(text, dim=VECTOR_DIM):
    """文本 → 归一化向量（list[float]）。"""
    counts = {}
    for tok, weight in _tokens(text):
        counts[tok] = counts.get(tok, 0.0) + weight
    vec = [0.0] * dim
    for tok, weight in counts.items():
        digest = hashlib.md5(tok.encode('utf-8')).digest()
        idx = int.from_bytes(digest[:4], 'little') % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign * (1.0 + math.log(1.0 + weight))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-9:
        vec = [v / norm for v in vec]
    return vec


def pack_vector(vec):
    return struct.pack('<%df' % len(vec), *vec)


def unpack_vector(blob, dim=VECTOR_DIM):
    if not blob:
        return [0.0] * dim
    n = min(dim, len(blob) // 4)
    vals = struct.unpack('<%df' % n, blob[:n * 4])
    return list(vals) + [0.0] * (dim - n)


def cosine(a, b):
    """两个已归一化向量的余弦（点积）。"""
    return sum(x * y for x, y in zip(a, b))


# ---------- 切片 ----------
def _sentences(text):
    """按断点切句，保留原文的标点与换行（不额外插入分隔符）。

    英文句点只有后面跟空白才断句，所以 0.0.0.0、a.b.com、3.14、v2.10.1
    这类内容会保持完整，不会在点号处被拆开。
    """
    out, start = [], 0
    for match in _BREAK.finditer(text):
        seg = text[start:match.end()]
        if seg.strip():
            out.append(seg)
        start = match.end()
    tail = text[start:]
    if tail.strip():
        out.append(tail)
    return out


def _tail(buf, overlap):
    """取上一块的尾部 overlap 字符作为下一块开头，尽量从句子边界起。"""
    if overlap <= 0 or not buf:
        return ''
    tail = buf[-overlap:]
    cut = max(tail.rfind('。'), tail.rfind('！'), tail.rfind('？'), tail.rfind('\n'))
    if cut > 0:
        tail = tail[cut + 1:]
    return tail.strip()


def split_text(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_CHUNK_OVERLAP):
    """按句子聚合切片：每片约 chunk_size 字符，片间保留 overlap 字符重叠。"""
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return []
    chunk_size = max(80, int(chunk_size or DEFAULT_CHUNK_SIZE))
    overlap = max(0, min(int(overlap or 0), chunk_size // 2))
    chunks, buf = [], ''
    for sent in _sentences(text):
        while len(sent) > chunk_size:
            if buf:
                chunks.append(buf.strip())
                buf = ''
            chunks.append(sent[:chunk_size].strip())
            sent = sent[chunk_size:]
            if len(chunks) >= MAX_CHUNKS_PER_DOC:
                break
        if not sent:
            continue
        if buf and len(buf) + len(sent) + 1 > chunk_size:
            chunks.append(buf.strip())
            buf = _tail(buf, overlap) + sent
        else:
            buf = (buf + sent) if buf else sent
        if len(chunks) >= MAX_CHUNKS_PER_DOC:
            break
    if buf.strip() and len(chunks) < MAX_CHUNKS_PER_DOC:
        chunks.append(buf.strip())
    return [c for c in chunks if c]


# ---------- 文档入库 ----------
def add_document(kb_id, title, content, source=''):
    """写入文档并建立切片索引，返回 (doc_id, chunk_count)。"""
    content = (content or '').strip()
    kb = query_one('SELECT * FROM knowledge_bases WHERE id=?', (kb_id,))
    if not kb:
        raise ValueError('知识库不存在')
    chunks = split_text(content, kb['chunk_size'], kb['chunk_overlap'])
    conn = get_conn()
    now = ts()
    try:
        cur = conn.execute(
            'INSERT INTO kb_documents (kb_id, title, source, content, char_count, '
            'chunk_count, created_at) VALUES (?,?,?,?,?,?,?)',
            (kb_id, title or '未命名文档', source or '', content, len(content),
             len(chunks), now))
        doc_id = cur.lastrowid
        rows = []
        for seq, chunk in enumerate(chunks):
            vec = embed(chunk)
            rows.append((kb_id, doc_id, seq, chunk, pack_vector(vec), len(vec)))
        if rows:
            conn.executemany(
                'INSERT INTO kb_chunks (kb_id, doc_id, seq, content, vector, dim) '
                'VALUES (?,?,?,?,?,?)', rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return doc_id, len(chunks)


def reindex_kb(kb_id):
    """按当前切片参数重建整个知识库索引，返回 (文档数, 切片数)。"""
    kb = query_one('SELECT * FROM knowledge_bases WHERE id=?', (kb_id,))
    if not kb:
        raise ValueError('知识库不存在')
    docs = query_all('SELECT id, content FROM kb_documents WHERE kb_id=?', (kb_id,))
    conn = get_conn()
    now = ts()
    try:
        conn.execute('DELETE FROM kb_chunks WHERE kb_id=?', (kb_id,))
        total = 0
        for doc in docs:
            chunks = split_text(doc['content'], kb['chunk_size'], kb['chunk_overlap'])
            rows = []
            for seq, chunk in enumerate(chunks):
                vec = embed(chunk)
                rows.append((kb_id, doc['id'], seq, chunk, pack_vector(vec), len(vec)))
            if rows:
                conn.executemany(
                    'INSERT INTO kb_chunks (kb_id, doc_id, seq, content, vector, dim) '
                    'VALUES (?,?,?,?,?,?)', rows)
            conn.execute('UPDATE kb_documents SET chunk_count=? WHERE id=?',
                         (len(chunks), doc['id']))
            total += len(chunks)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(docs), total


def update_document(doc_id, title, content):
    """更新文档标题/内容，并按知识库当前切片参数重建该文档的索引。

    返回新的切片数；文档不存在、知识库不存在或内容为空时抛 ValueError。
    """
    doc = query_one('SELECT * FROM kb_documents WHERE id=?', (doc_id,))
    if not doc:
        raise ValueError('文档不存在')
    kb = query_one('SELECT * FROM knowledge_bases WHERE id=?', (doc['kb_id'],))
    if not kb:
        raise ValueError('知识库不存在')
    content = (content or '').strip()
    if not content:
        raise ValueError('文档内容不能为空')
    chunks = split_text(content, kb['chunk_size'], kb['chunk_overlap'])
    conn = get_conn()
    try:
        conn.execute('UPDATE kb_documents SET title=?, content=?, char_count=?, '
                     'chunk_count=? WHERE id=?',
                     ((title or doc['title']), content, len(content), len(chunks), doc_id))
        conn.execute('DELETE FROM kb_chunks WHERE doc_id=?', (doc_id,))
        rows = []
        for seq, chunk in enumerate(chunks):
            vec = embed(chunk)
            rows.append((doc['kb_id'], doc_id, seq, chunk, pack_vector(vec), len(vec)))
        if rows:
            conn.executemany(
                'INSERT INTO kb_chunks (kb_id, doc_id, seq, content, vector, dim) '
                'VALUES (?,?,?,?,?,?)', rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(chunks)


def delete_document(doc_id):
    execute('DELETE FROM kb_documents WHERE id=?', (doc_id,))


# ---------- 检索 ----------
def search(kb_ids, query, top_k=DEFAULT_TOP_K, min_score=DEFAULT_MIN_SCORE):
    """在多个知识库中检索，返回按相似度倒序的命中列表。"""
    query = (query or '').strip()
    if not query or not kb_ids:
        return []
    qv = embed(query)
    hits = []
    for kb_id in kb_ids:
        rows = query_all(
            'SELECT c.id, c.content, c.vector, c.dim, c.doc_id, d.title, d.source '
            'FROM kb_chunks c JOIN kb_documents d ON d.id = c.doc_id '
            'WHERE c.kb_id=?', (kb_id,))
        for r in rows:
            score = cosine(qv, unpack_vector(r['vector'], r['dim'] or VECTOR_DIM))
            if score >= min_score:
                hits.append({'chunk_id': r['id'], 'kb_id': kb_id, 'doc_id': r['doc_id'],
                             'title': r['title'], 'source': r['source'],
                             'content': r['content'], 'score': round(score, 4)})
    hits.sort(key=lambda h: h['score'], reverse=True)
    return hits[:max(1, int(top_k or DEFAULT_TOP_K))]


def query_from_messages(messages, max_chars=1200):
    """从对话里提取检索关键词：以最后一条 user 文本为主，前一句作补充。"""
    users = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get('role') != 'user':
            continue
        content = m.get('content')
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            txt = ' '.join(str(p.get('text') or '') for p in content
                           if isinstance(p, dict))
        else:
            txt = ''
        txt = txt.strip()
        if txt:
            users.append(txt)
    if not users:
        return ''
    if len(users) >= 2:
        text = users[-2][-300:] + '\n' + users[-1]
    else:
        text = users[-1]
    return text[:max_chars]


def build_context(hits, max_chars=3000):
    """把命中片段拼成一段 system 提示。"""
    if not hits:
        return ''
    lines = ['【知识库资料】以下是检索到的相关资料。回答时优先依据这些资料，'
             '并在引用处用 [编号] 标注来源；资料没有覆盖的内容可以结合常识回答，'
             '但不要编造资料里不存在的事实。']
    for i, h in enumerate(hits, 1):
        lines.append('[%d] 来源：%s\n%s' % (i, h['title'] or '未命名文档', h['content']))
    return '\n\n'.join(lines)[:max(200, int(max_chars or 3000))]


def merge_system(messages, context_text):
    """把知识库上下文并入 messages（优先合并到已有的第一条 system，避免破坏上游格式）。"""
    if not context_text:
        return list(messages or [])
    msgs = [dict(m) if isinstance(m, dict) else m for m in (messages or [])]
    for m in msgs:
        if isinstance(m, dict) and m.get('role') == 'system':
            old = m.get('content')
            if isinstance(old, str):
                m['content'] = (old.rstrip() + '\n\n' + context_text).strip()
            elif isinstance(old, list):
                m['content'] = old + [{'type': 'text', 'text': context_text}]
            else:
                m['content'] = context_text
            return msgs
    return [{'role': 'system', 'content': context_text}] + msgs


def _positive_int(value, default, maximum):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


def _score(value):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_SCORE


def rag_enrich(messages, kb_ids, top_k=None, min_score=None, max_chars=None):
    """检索 + 注入，返回 (新 messages, hits)。

    top_k / min_score 传 None 时使用**每个知识库自己的配置**；传入则覆盖所有库。
    多个知识库按各自参数检索后合并，再按分数取全局前 N 条，避免一个库的
    低分结果挤掉另一个库的高分结果。
    """
    if not kb_ids:
        return list(messages or []), []
    query = query_from_messages(messages)
    if not query:
        return list(messages or []), []
    hits = []
    limit = 0
    for kb_id in kb_ids:
        kb = query_one('SELECT top_k, min_score, enabled FROM knowledge_bases WHERE id=?',
                       (kb_id,))
        if not kb or not kb['enabled']:
            continue
        k = _positive_int(top_k if top_k is not None else kb['top_k'], DEFAULT_TOP_K, 20)
        s = _score(min_score if min_score is not None else kb['min_score'])
        limit = max(limit, k)
        hits.extend(search([kb_id], query, k, s))
    if not hits:
        return list(messages or []), []
    hits.sort(key=lambda h: h['score'], reverse=True)
    take = _positive_int(top_k, limit or DEFAULT_TOP_K, 20)
    return merge_system(messages, build_context(hits, max_chars)), hits[:take]


# ---------- 上传文本解析 ----------
_TEXT_EXT = ('.txt', '.md', '.markdown', '.csv', '.json', '.log', '.ini', '.yaml', '.yml')


def extract_text(filename, data):
    """从上传文件里抽取纯文本（支持 txt/md/csv/json 与可选 pypdf 解析 PDF）。"""
    name = (filename or '').lower()
    ext = os.path.splitext(name)[1]
    if ext == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError('未安装 pypdf，无法解析 PDF。可先 `pip install pypdf`，'
                             '或把内容复制成纯文本上传')
        reader = PdfReader(io.BytesIO(data))
        return '\n'.join((page.extract_text() or '') for page in reader.pages).strip()
    if ext == '.docx':
        try:
            import docx
        except ImportError:
            raise ValueError('未安装 python-docx，无法解析 Word。可先 '
                             '`pip install python-docx`，或转成纯文本上传')
        document = docx.Document(io.BytesIO(data))
        return '\n'.join(p.text for p in document.paragraphs).strip()
    if ext and ext not in _TEXT_EXT:
        raise ValueError('不支持的文件类型：%s（支持 txt/md/csv/json/pdf/docx）' % name)
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'latin-1'):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    raise ValueError('文件编码无法识别，请转成 UTF-8 后重试')
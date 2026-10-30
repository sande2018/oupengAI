# 欧朋AI · 多模型智能调度网关

一个用 **Python + Flask + SQLite** 写成的 AI 网关：把多个上游 OpenAI 兼容服务（官方、中转、自建）登记为「渠道」，每个渠道可放多组 API Key、多个模型；模型上勾选能力类型，再用一个自定义模型名把它们组成调度组对外提供。客户端只认一个模型名，后台自动完成选型、故障切换、Key 轮换与知识库检索。

---

## 1. 功能一览

| 能力类型 | 触发方式 | 默认走的上游接口 |
| --- | --- | --- |
| `text` 文本 | 纯文本 chat | `/chat/completions` |
| `vision` 图片理解 | messages 里含 `image_url` | `/chat/completions`（多模态） |
| `ocr` 图片识别 | 含图片且提示词是识别意图，或 `X-Task-Type: ocr` | `/chat/completions` |
| `gen` 生图 | `POST /v1/images/generations` | `/images/generations` |
| `video_gen` 生视频 | `POST /v1/videos/generations` | 渠道里可配置的 `video_path` |
| `video_vision` 视频识别 | messages 里含 `video_url` | `/chat/completions`（多模态） |

其它能力：

- **多 Key 轮换**：同一渠道填多组 Key，按请求轮换起点；某个 Key 返回 401/403/429 只冷却它自己，不影响同渠道其它 Key。
- **故障切换**：模型 5xx / 超时 / 连不上 → 自动换下一个渠道模型；连续失败 5 次熔断该渠道 60s。
- **对外模型名**：`model=oupeng` 这类自定义名字，后台勾选参与调度的模型；策略支持 `priority`（按优先级故障切换）与 `weighted`（按权重轮询负载均衡）。
- **向量知识库**：完全离线的本地哈希向量（512 维），支持切片、检索测试、重建索引；API Key 可绑定一个或多个知识库，对话时自动检索并注入 system。
- **OpenAI 兼容**：`/v1/chat/completions`（含流式）、`/v1/images/generations`、`/v1/videos/generations`、`/v1/embeddings`、`/v1/models`。
- **后台管理**：渠道、模型、调度组、知识库、API Key、调用日志、在线测试、系统设置，全部内置，不依赖任何 CDN。

---

## 2. 快速开始

```powershell
cd E:\PythonFiles\欧朋AI
python -m pip install -r requirements.txt
python app.py
```

打开 <http://127.0.0.1:8000/db_admin/>，首次密码 `admin888`（登录后到「系统设置」修改）。

启动时会自动创建 `data/oupeng.db`，并预置一个对外模型 `oupeng`（未勾选任何目标时自动使用全部启用模型，方便先跑通）。

Windows 也可以双击 `start.bat`。

---

## 3. 五步配置

### ① 渠道与 Key（`/db_admin/channels`）

- **Base URL 写到版本号**：`https://api.openai.com/v1`、`https://your-relay.com/v1`。
- **多组 Key**：一组一行，可命名；Key 只存在本地 SQLite，列表页只显示 `sk-abc…1234`。
- **生视频端点**：默认 `/videos/generations`，部分服务用 `/videos` 或 `/video/generations`，按上游文档改。
- 「测试」按钮会对该渠道的前几个模型各发一次真实请求，显示耗时和回复。
- 状态徽标可点击：快速启用/停用渠道（会二次确认，停用后该渠道所有模型都不参与调度）。

### ② 添加渠道模型（`/db_admin/models` → 渠道模型）

填上游真实模型名（如 `gpt-4o-mini`、`claude-3-5-sonnet`、`gemini-2.0-flash`），勾选能力类型：

- 一个多模态模型可以同时勾「文本」+「图片理解」；
- 专门做 OCR 的模型勾「图片识别」；
- 生图模型勾「生图」，生视频模型勾「生视频」。
- 模型列表里的状态徽标也可点击快速启停（二次确认，类型与其它配置保留）。
- 手动新增/编辑模型的弹窗里，勾选能力类型后可直接点「测试选中类型」，按当前填写的渠道与模型名真实请求一次；通过的类型变绿、失败变红，鼠标悬停可看返回内容或错误。

也可以直接点「从上游获取模型」：选择渠道 → 自动请求上游 `GET /models` → 勾选模型、确认（可修改）能力类型 → 批量保存。已导入的模型标记「已存在」，已停用的标记「已停用」；取消勾选一个已存在的模型并保存，会把它**停用**（类型保留），重新勾选并保存则恢复启用。渠道列表里的「拉模型」按钮会直接跳到这个弹窗。

类型会按模型名给出「建议」（仅在表格里展示，默认不勾选，需手动选择）：`*-vl` / `4o` / `gemini` / `claude-3+` 等建议「文本 + 图片理解」，`*ocr*` 建议「图片识别」，`dall-e` / `flux` / `gpt-image` 等建议「生图」，`sora` / `veo` / `kling` 等建议「生视频」，`*embedding*` 无建议（不属于六类能力）。

表格里每行还有「测试」列：点一下会按该行勾选的能力类型各发一次真实请求，通过的类型标签变绿、失败变红，鼠标悬停可看到模型返回内容或错误详情。测试请求不写 Key 冷却与渠道熔断状态，不会影响线上调度；视频识别测试可在弹窗顶部填视频 URL（默认一个公开示例）。六类能力的测试案例统一维护在 `services/testcases.py`：
- 文本：一句短问题，要求只回复两个字
- 图片理解：一张红色方块图，问颜色
- 图片识别：一张写有 `OUPENG 2026` 的白底黑字图，要求识别文字
- 生图：`a red apple on a white table, product photo`
- 生视频：`a cat walking in the snow`
- 视频识别：公开示例视频（Big Buck Bunny 10s）

在线测试页选择模型后，会在下拉框下方显示该模型对应的能力类型：自定义模型显示其启用调度目标的类型并集，未限制目标时显示「自动匹配」；上游真实模型显示它勾选的类型。

### ③ 建对外模型（`/db_admin/models` → 对外模型）

- 模型名就是客户端请求里的 `model`，例如 `oupeng`、`chat-pro`。
- 勾选参与调度的渠道模型，可设置每个目标的优先级/权重。
- `priority`：按优先级从小到大依次尝试，前一个失败自动切下一个。
- `weighted`：同优先级内按权重平滑轮询，适合多家渠道做负载均衡。

### ④ 知识库（可选，`/db_admin/knowledge`）

- 新建知识库 → 上传 `txt/md/csv/json`（装 `pypdf` 后支持 PDF，装 `python-docx` 后支持 Word），或直接粘贴文本。
- 「检索测试」里输入一句话，可看到命中片段与相似度。
- 文档列表支持「编辑」：修改标题或正文后保存，会按知识库当前的切片参数重建该文档的索引。
- 编辑弹窗里可以选择一个模型做「AI 润色」：按主题分段、每段自包含、去口语与重复，保留事实与数字；结果只填回编辑框，确认后保存才生效。单次最多 **60000 字**；超过 6000 字会自动按句子分批调用（每批约 6000 字、最多 12 批），再拼接返回，因此不会受模型上下文限制。
- 调整切片大小/重叠后会自动重建索引；也可手动点「重建索引」。

### ⑤ 站内 API Key（`/db_admin/apikeys`）

- 生成 `sk-ou-...`，可设置每分钟限速，可勾选关联的知识库（可多选）。
- 页面顶部直接显示 Base URL 并支持一键复制（反向代理场景请确认代理转发的 Host 正确）。
- 用这个 Key 调用 `/v1/*` 时，如果关联了知识库，chat 请求会自动做 RAG 注入。
- 支持「重置」生成新 Key（旧的立即失效）。

---

## 4. 智能调度的判定规则

### 4.1 类型判定（chat/completions）

按优先级：

1. 请求头 `X-Task-Type: text|vision|ocr|gen|video_gen|video_vision`
2. 请求体扩展字段 `"task_type": "ocr"`（转发上游前会被剔除）
3. 内容判定：
   - 含 `video_url` → `video_vision`
   - 含 `image_url` → 提示词命中 OCR 意图关键词（「识别文字」「提取文字」「图片转文字」「票据识别」「小票」等）→ `ocr`，否则 `vision`
   - 纯文本 → `text`

### 4.2 兜底链

- `vision` → `ocr`（没配视觉模型时，OCR 模型也能看普通图片）
- `ocr` → `vision`（OCR 模型挂了，退普通视觉模型）
- `text` → `vision`（纯文本请求也能交给视觉模型处理，参考 `ai.py` 的做法）
- `gen` / `video_gen` / `video_vision` 不兜底

### 4.3 候选展开与失败处理

一次请求会展开为：`渠道模型 × 渠道内可用 Key`，按策略排序后逐个尝试。

| 上游返回 | 处理 |
| --- | --- |
| 200 | 成功，清零该 Key / 渠道失败计数 |
| 401 / 403 / 402 | 冷却该 Key 5 分钟，继续下一个候选 |
| 429 | 按 `Retry-After` 或 60s 冷却该 Key |
| 400 / 404 / 413 / 422 | 参数或模型名问题，跳过该候选并记模型失败 |
| 5xx / 超时 / 连接错误 | 渠道失败计数 +1；连续 5 次熔断 60s |
| 全部失败 | 返回 502，`error.message` 里带最近 3 次尝试的摘要 |

**Key 轮换**：每次请求把该渠道的 Key 起点前移一位。全部 Key 都在冷却时，会取最早解冻的那个做半开尝试，避免渠道整体不可用。

**熔断半开**：处于冷却期的渠道只会被排到候选末尾，不会彻底剔除，冷却结束后自动恢复。

---

## 5. 调用示例

### 5.1 curl

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-ou-你的Key" \
  -H "Content-Type: application/json" \
  -d '{"model":"oupeng","messages":[{"role":"user","content":"你好"}]}'
```

### 5.2 Python（openai SDK）

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-ou-你的Key")

# 纯文本 → text
print(client.chat.completions.create(
    model="oupeng",
    messages=[{"role": "user", "content": "用三句话介绍 SQLite"}],
).choices[0].message.content)

# 带图片 → vision（提示词是识别意图时自动转 ocr）
resp = client.chat.completions.create(
    model="oupeng",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "这张图里有什么？"},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}},
    ]}],
)

# 流式
stream = client.chat.completions.create(
    model="oupeng",
    messages=[{"role": "user", "content": "讲个笑话"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")

# 生图 → gen
img = client.images.generate(model="oupeng", prompt="一只在雪地里的橘猫", n=1)
print(img.data[0].url)
```

### 5.3 扩展参数（不会转发给上游）

```json
{
  "model": "oupeng",
  "messages": [{"role": "user", "content": "退款要几天"}],
  "knowledge": {"kb_ids": [1, 2], "top_k": 5, "max_chars": 2500}
}
```

- `"knowledge": false`：本次关闭知识库。
- 请求头 `X-Knowledge: off`：同上。
- 请求头 `X-Knowledge-Ids: 1,2`：临时指定知识库（覆盖 API Key 绑定）。
- 请求头 `X-Task-Type: ocr`：强制走 OCR 类模型。
- `"task_type": "vision"`：请求体里强制类型。

### 5.4 响应头

| 响应头 | 含义 |
| --- | --- |
| `X-Ou-Model` | 实际上游模型名（URL 编码） |
| `X-Ou-Channel` | 实际命中的渠道名（URL 编码） |
| `X-Ou-Type` | 本次调度的能力类型 |
| `X-Ou-Rag-Hits` | 知识库命中条数 |
| `X-Ou-Latency-Ms` | 总耗时（含故障切换） |

响应体的 `model` 字段会被回填为客户端请求的模型名（流式响应除外，流式按上游原样透传）。

### 5.5 其它端点

```bash
GET  /v1/models                       # 对外模型 + 上游真实模型
GET  /v1/models/{id}
POST /v1/images/generations           # 生图
POST /v1/videos/generations           # 生视频（/v1/video/generations 同样可用）
POST /v1/embeddings                   # 本地哈希向量，OpenAI 格式
GET  /v1/capabilities                 # 六类能力是否已配置
```

`/v1/embeddings` 完全本地计算，返回 512 维向量，不消耗上游额度，可用于自建索引。

---

## 6. 知识库原理与调参

本地哈希向量（`services/knowledge.py`）：

1. 中文按连续汉字段切分，取单字（权重 0.35）与二元组（权重 1.0）；英文/数字按词切分（权重 1.6）。
2. 用 MD5 前 4 字节把 token 哈希到 512 维空间的某一维，再用第 5 个字节决定正负号（hashing trick，避免 Python 内置 hash 的进程随机化）。
3. 词频做 `1 + log(1 + tf)` 压缩，最后 L2 归一化，检索时余弦相似度 = 点积。
4. 向量以 float32 小端 BLOB 存 SQLite，检索时按知识库全表扫描。

切片规则：中文按句末标点与换行断句；英文句点只有**后面跟空白**时才算句末，所以 `0.0.0.0`、域名、URL、`19.9`、`v2.10.1` 这类内容不会被拆碎，切块之间也不会被额外插入换行。

默认最低相似度是 **0.05**：哈希向量的分数普遍偏低，0.12 会把「怎么退款」「我想退货」这类正常问法漏掉。每个知识库自己的 `top_k` / `min_score` 会直接用于 API 调用的 RAG，系统设置里的值只作为新建知识库的默认值；请求体 `knowledge` 里的参数可临时覆盖。

调参建议：

- 召回太少：降低最低相似度（如 0.05）、提高召回条数、把文档拆细。
- 召回太杂：提高最低相似度（0.2~0.3）、缩短切片、开启更严格的提示词。
- 长文档：切片 500 字、重叠 80 字起步；问答型资料可降到 200~300 字。
- 重新上传或改切片参数后记得「重建索引」。

关于质量：哈希向量靠字面重合，适合 FAQ、制度、产品手册这类「关键词能对上」的资料；语义改写能力弱于神经网络 embedding。如果以后想升级，只需替换 `knowledge.embed()` 的实现（例如调用一个 `/v1/embeddings` 渠道模型），其它代码不用动。

RAG 注入规则：

- 只在 `text` / `vision` 请求上生效（`ocr`、`gen`、`video_*` 不注入）。
- 召回条数与最低相似度以每个知识库自己的配置为准；请求体 `knowledge.top_k / min_score` 可临时覆盖。
- 检索词取最后一条 user 文本（有上一轮时附带其尾部 300 字）。
- 资料以 system 消息注入；已有 system 时合并到同一条，避免某些上游只接受一条 system。
- 注入内容带编号与来源，提示模型用 `[1]` 形式标注。

---

## 7. 目录结构

```
欧朋AI/
├── app.py                     # Flask 应用入口、CORS、模板过滤器
├── config.py                  # 配置与六类能力定义
├── db.py                      # SQLite 建表、连接管理、查询 helper
├── requirements.txt
├── start.bat
├── services/
│   ├── router.py              # 调度核心：候选展开、Key 轮换、熔断、上游请求
│   └── knowledge.py           # 切片、哈希向量、检索、RAG 注入
├── routes/
│   ├── openai_api.py          # /v1/* OpenAI 兼容端点
│   ├── admin_api.py           # /db_admin/api/* 管理接口
│   └── admin_pages.py         # /db_admin/* 页面路由
├── templates/                 # 登录、概览、渠道、调度、知识库、Key、日志、测试、设置
├── static/
│   ├── css/app.css
│   └── js/app.js              # fetch 封装、Toast、弹窗、复制、类型徽标
├── tests/
│   ├── mock_upstream.py       # 假上游（按模型名/Key 返回不同结果）
│   ├── test_gateway.py        # 端到端：路由、故障切换、Key 冷却、RAG、流式
│   └── test_admin.py          # 后台页面渲染 + 完整 CRUD
└── data/                      # 运行后生成：oupeng.db、uploads/
```

数据库表：

`channels`、`channel_keys`、`channel_models`、`custom_models`、`custom_model_targets`、
`knowledge_bases`、`kb_documents`、`kb_chunks`、`api_keys`、`api_key_kbs`、`call_logs`、`settings`。

---

## 8. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OUPENG_HOST` | `0.0.0.0` | 监听地址 |
| `OUPENG_PORT` | `8000` | 监听端口 |
| `OUPENG_DEBUG` | 关 | `1` 开启 Flask debug |
| `OUPENG_DATA_DIR` | `./data` | 数据目录 |
| `OUPENG_DB_PATH` | `./data/oupeng.db` | SQLite 路径 |
| `OUPENG_SECRET_KEY` | 开发默认值 | **生产必须改** |
| `OUPENG_ADMIN_PASSWORD` | `admin888` | 首次启动创建的管理员密码 |
| `OUPENG_CONNECT_TIMEOUT` | `10` | 连接上游超时（秒） |
| `OUPENG_UPSTREAM_TIMEOUT` | `180` | 单次请求超时（渠道可覆盖） |
| `OUPENG_RETRY_PER_CANDIDATE` | `0` | 单个候选内部重试次数 |
| `OUPENG_MAX_REQUEST_BYTES` | 64MB | 请求体上限 |

---

## 9. 测试

```powershell
python -m unittest discover -s tests -t . -v
```

35 项测试，覆盖：

- 鉴权（缺 Key / 错 Key / 停用）
- 文本、图片理解、OCR（提示词与请求头两种判定）、生图、生视频的路由
- 主渠道 500 自动切备用渠道
- 多 Key 轮换与 401 冷却
- 视觉 → OCR 兜底链
- 流式 SSE 透传
- 知识库入库、检索、按 API Key 绑定注入、`X-Knowledge: off`、`knowledge.kb_ids` 指定库
- 优先级的排序、加权轮询负载均衡
- 后台全部页面渲染与完整 CRUD 流程（含 Key 脱敏、密码修改）

测试用进程内假上游（`tests/mock_upstream.py`）替代真实 HTTP，因此在无外网/无 localhost 连接的环境也能跑。

---

## 10. 部署与安全

1. **改密码、改 SECRET_KEY**：默认密码只用于首次登录。
2. 用生产 WSGI 服务器，不要用 `app.run`：
   ```powershell
   python -m pip install waitress
   waitress-serve --listen=0.0.0.0:8000 --channel-timeout=900 app:app
   ```
   长文档 AI 润色会分批串行调用模型，耗时可能到几分钟，`--channel-timeout` 和反向代理的 `proxy_read_timeout` 都要相应调大。
3. **单进程多线程**运行（`waitress --threads=8` 或 gunicorn `--workers 1 --threads 8`）。
   Key 轮换与熔断状态存在进程内存里，多进程会各自计数。
4. 反向代理（Nginx/Caddy）加 HTTPS，限制 `/db_admin` 只允许内网访问。
5. 备份 `data/oupeng.db`（WAL 模式下同时备份 `-wal`/`-shm` 或先停服）。
6. 站内 API Key 只以明文存在本地库（便于后台复制查看）；如果多租户使用，建议加一层加密或定期轮换。
7. 后台依赖同源 Cookie，未开放 CORS；`/v1/*` 开放跨域但只认 Authorization Key，不依赖 Cookie。

---

## 11. 常见问题

**Q：上游报 400 / 404，日志里能看到什么？**
到「调用日志」筛选失败，`错误` 列是上游返回的前 300 字符；`渠道 / 上游模型` 列显示是哪一家、哪个模型、哪组 Key。常见原因是模型名写错、上游不支持 `thinking`/`stream_options` 等参数。

**Q：生视频一直失败？**
先在渠道里确认「生视频端点」，再点该模型的「测试」（选择生视频）。不同服务对 `prompt`、`image`、`duration` 参数要求不同，测试失败时把上游报错按需补参数即可。

**Q：从上游拉取模型失败？**
先确认 Key 有 `GET /models` 权限（返回 401 就是 Key 无效或额度问题）；Base URL 末尾没写 `/v1` 时程序会自动再试一次 `/v1/models`；个别中转不提供模型列表接口，只能手填。

**Q：API Key 绑定了知识库，但看起来没生效？**
按顺序检查：① Key 是否在「API Key → 编辑」里保存了关联（列表会显示「库名 · N 切片」）；② 知识库的切片数是否大于 0；③ 请求是不是 chat（OCR、生图、视频不注入）；④ 响应头 `X-Ou-Rag-Hits` 是否为 0，若是就到「知识库 → 检索测试」看相似度，适当降低最低相似度；⑤ 用「在线测试」时确认勾选了「使用知识库增强」。

**Q：带图片的请求被分到 OCR 模型了？**
提示词触发了 OCR 关键词（如「识别」「提取文字」）。想强制视觉理解就加请求头 `X-Task-Type: vision`。

**Q：知识库明明有文档，却没命中？**
「检索测试」里看相似度；低于知识库的最低相似度就不会注入。降低阈值或换更贴近原文的问法。

**Q：流式响应里 model 还是上游真实模型名？**
流式为了保证 SSE 字节原样，不改写 chunk。可看响应头 `X-Ou-Model` / `X-Ou-Channel`。

**Q：能不能再加「图片编辑 / 图生图」？**
可以。在 `config.AI_TYPES` 增加 `image_edit`，在 `router` 里加对应类型，再加一个 `/v1/images/edits` 转发端点即可；现有调度、Key 轮换、熔断都会自动复用。
# OupengAI · 多模型智能调度网关

一个用 **Python + Flask + SQLite** 写成的 AI 网关：把多个上游 OpenAI 兼容服务（官方、中转、自建）登记为「渠道」，每个渠道可放多组 API Key、多个模型；模型上勾选能力类型，再用一个自定义模型名把它们组成调度组对外提供。客户端只认一个模型名，后台自动完成选型、故障切换、Key 轮换与知识库检索。

- 本次V1.2.0 更新了众多内容：
  - 支持一键生成插件、智能体。并且以JS插件的形式，展示在任意网站中，可以当网页的智能客服。
  - 每个APIkey可以单独绑定智能体、插件、知识库。
  - 可以为每个渠道单独设置到期时间，以及每个模型的额度上限。
  - Windows最新版请查看[Releases-V1.2.0](https://github.com/sande2018/oupengAI/releases/tag/V1.2.0)
  <img width="264" height="122" alt="image" src="https://github.com/user-attachments/assets/1dcde3b2-20af-4fd9-82a8-39edc9285397" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/a1937510-a38f-4b45-8db8-af4d053a0405" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/33d681b1-07ff-4383-b60a-74f3a87e6862" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/539e7797-16d3-45cd-9c41-f3dc2150b36f" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/3e2677bb-c6df-44e6-b95a-a0d951506144" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/53dbae74-e18d-436a-9797-f137e7ac521f" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/6fe41ec0-4e71-49b4-88e5-95596696dd03" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/1fa187d1-d23f-49a2-a267-4eb99b9357c4" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/e55e3acf-abc4-4c0c-bfae-bfc4b522386e" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/aabd119d-8d0a-41d2-b9e7-56ff0ab5a7de" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/59d485f3-2c31-436d-81cc-f015da75050a" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/3a2935a8-8f68-4f9b-887f-baefddd6834b" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/29be2406-4d4f-4e15-b86e-f6ad2369583f" />

<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/85bd64da-b82b-4d16-8868-dd99e0e1c5cc" />

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
- **智能体**：每个智能体有独立的系统 Prompt，可加入多个插件、关联多个知识库，并可指定默认模型；绑定到 API Key 后，该 Key 的所有 chat 请求自动套用。
- **插件库**：插件 = 一段可复用的提示词，可选附带一个 HTTP 工具（function calling）。可直接把多个插件关联到 API Key（不必建智能体），也可以由智能体统一管理。模型发起工具调用后由网关代为请求目标地址、把结果回填给模型，最多自动执行 3 轮。
- **网页客服插件**：两种接入方式二选一——嵌入一行云端脚本（右下角气泡球、可收缩、支持图片与多轮），或直接使用在线链接（打开即是完整客服页面，可发给客户）。窗口参数都由网关托管并实时下发，网页源码里没有 API Key；支持上架 / 下架 / 重置地址，也可随时回到后台改配置。
- **OpenAI 兼容**：`/v1/chat/completions`（含流式）、`/v1/images/generations`、`/v1/videos/generations`、`/v1/embeddings`、`/v1/models`。
- **后台管理**：渠道、模型、调度组、知识库、智能体、客服插件、插件库、API Key、调用日志、在线测试、系统设置，全部内置，不依赖任何 CDN。

---

## 2. 快速开始

```powershell
python -m pip install -r requirements.txt
python app.py
```

打开 <http://127.0.0.1:8000/db_admin/>，初始超级管理员账号 `yb_admin`，初始密码 `123456`（登录后到「系统设置」修改）。

> 只有数据库中还没有 `yb_admin` 时才会用初始密码创建账号；已有账号不会被覆盖或重置。登录页的初始账号提示只在密码仍为初始值时显示，修改密码后自动消失。

启动时会自动创建 `data/oupeng.db`，并预置一个对外模型 `oupeng`（未勾选任何目标时自动使用全部启用模型，方便先跑通）。

Windows 也可以双击 `start.bat`。

### 多用户与共享

- 登录页支持用户名 + 密码，并可自助注册（「系统设置」可关闭注册）；超级管理员也可以在「用户管理」新增用户、重置密码、停用或删除账号。
- 每个用户的数据完全独立：渠道、对外模型、智能体、插件、知识库、API Key 与调用日志都按所有者隔离。
- 插件库 / 知识库：所有者点「申请共享」，管理员在「共享审核」通过后其他用户才能看到（只读可用，不能修改）；未审核或已拒绝时只有本人可见；所有者可随时停止共享。
- 智能体：审核通过后进入「应用市场」，其他用户需要手动「安装」才会在自己的智能体库里生成一份可编辑副本。安装后即为自己的数据，可以修改或删除；插件 / 知识库会“跟随回源”原作者，即使作者没有单独共享，副本也能正常使用（依赖内容仍为只读）。作者私有的模型在当前账号不可用时会改为跟随请求。安装副本不是原创，不能再次申请共享。
- 生成客服插件时，API Key 下拉框只显示当前账号自己的 Key，不能选择或绑定其他用户的 Key。
- 渠道：所有者可随时「共享 / 停止共享」，无需审核。共享后其他用户可在「智能调度」里勾选该渠道的模型；停止共享后立即从其他用户的调度候选中移除。共享渠道产生的调用额度记在渠道所有者名下。
- 安装后的智能体副本按安装者自己的模型与渠道范围运行；调用日志记在调用者账号下。

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
- **用量与额度（最高一级）**：列表按「万」显示每个模型的 token 已用量，弹窗可单独设置到期日与剩余 token（留空 = 不限，0 = 已用完）。渠道模型额度优先于调度组：剩余量用完或到期后会**自动切换为停用状态**（该模型不再参与任何调度），补充剩余 token 或延长到期日后可重新启用。

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
- **用量与额度**：列表里按「万」显示整组和每个模型的 token 已用量；编辑弹窗可以给整组和每个模型单独设置到期日与剩余 token（留空 = 不限，0 = 已用完）。任一层到期或用完后自动停止调度：模型级停止只影响它自己，组级停止则整个调度组不再服务。用量按上游返回的 `usage` 统计，流式响应上游没给 `usage` 时按文本长度估算。

### ④ 知识库（可选，`/db_admin/knowledge`）

- 新建知识库 → 上传 `txt/md/csv/json`（装 `pypdf` 后支持 PDF，装 `python-docx` 后支持 Word），或直接粘贴文本。
- 「检索测试」里输入一句话，可看到命中片段与相似度。
- 文档列表支持「编辑」：修改标题或正文后保存，会按知识库当前的切片参数重建该文档的索引。
- 编辑弹窗里可以选择一个模型做「AI 润色」：按主题分段、每段自包含、去口语与重复，保留事实与数字；结果只填回编辑框，确认后保存才生效。单次最多 **60000 字**；超过 6000 字会自动按句子分批调用（每批约 6000 字、最多 12 批），再拼接返回，因此不会受模型上下文限制。
- 同一弹窗还能填写「修改需求」（如「把退款条款改写得更口语化，并补充退款时效」），点「按需求修改」让模型按需求改写正文；需求最多 2000 字，结果同样只填回编辑框。
- 调整切片大小/重叠后会自动重建索引；也可手动点「重建索引」。

### ⑤ 站内 API Key（`/db_admin/apikeys`）

- 生成 `sk-ou-...`，可设置每分钟限速，可勾选关联的知识库（可多选）。
- **关联插件库（可多选）**：即使不绑定智能体，模型也能直接调用这些插件里的 HTTP 工具——网关把插件提示词拼进 system，注入 `tools`，并代执行 `tool_calls`（最多 3 轮）。绑定了智能体时，两边的插件会自动合并去重（智能体的在前）。客户端自己在请求体里传 `tools` 时网关不接管，保持原样透传。停用的插件不注入。
- 可直接把 Key 绑定到一个智能体（默认 Prompt / 插件 / 知识库自动生效）。
- **可用模型与额度**：可勾选这个 Key 能用的**对外模型**与**渠道模型**（两类都不勾 = 全部模型可用，默认如此）；还能设置「剩余 Tokens（万）」和「到期日」，留空 = 不限。额度用完或到期后，这个 Key 的所有请求都会被直接拒绝（403），`/v1/models` 也只返回被授权的模型。
- 页面顶部直接显示 Base URL 并支持一键复制（反向代理场景请确认代理转发的 Host 正确）。
- 用这个 Key 调用 `/v1/*` 时，如果关联了知识库，chat 请求会自动做 RAG 注入。
- 支持「重置」生成新 Key（旧的立即失效）。

### ⑥ 智能体与插件（可选，`/db_admin/agents`、`/db_admin/plugins`）

顺序：先在「插件库」建插件 → 在「智能体」里加入插件/知识库、写系统 Prompt → 在「API Key」里绑定智能体，或直接把插件关联到 API Key（不建智能体也能用工具）。

**AI 自动填写**：智能体与插件的编辑弹窗顶部都有一段「需求描述」输入框。粘贴一段多行文字（例如「做一个电商客服助手，回答简洁礼貌，遇到订单问题先让用户提供订单号」），选择模型或留空自动选择，点「AI 填写」即可生成名称、说明、系统 Prompt、采样参数或工具定义。AI 只填写表单，不会自动保存，也不会改动已勾选的插件/知识库；不确定的接口地址会留空，需要你补充。

**插件**由两部分组成，都可留空：

- **提示词**：拼进智能体 system 的提示片段（例如「回答前先确认城市」）。
- **HTTP 工具**：工具名、给模型看的说明、参数 JSON Schema、请求方法（GET/POST）、执行地址、请求头、超时。
  模型返回 `tool_calls` 后，网关以 JSON 请求目标地址，把响应正文作为 `role=tool` 消息回填，再请求模型，最多 3 轮。
  客户端自己传 `tools` 时网关不接管，保持原样透传。

**智能体**可配置：

- **系统 Prompt**：放在所有消息最前，优先级高于客户端自带的 system。
- **默认模型**：留空则沿用请求里的 `model`；填写后该智能体所有请求统一走这个模型。
- **Temperature / Max Tokens**：`-1` / `0` 表示不覆盖，客户端显式传了值时以客户端为准。
- **插件与知识库**：可多选；知识库检索结果会追加在 Prompt 之后。

**注入顺序**：智能体 Prompt → 插件提示词 → 知识库资料。

**知识库优先级**：请求头 `X-Knowledge-Ids` / 请求体 `knowledge.kb_ids` > 智能体关联 > API Key 关联。

**流式**：智能体启用了 HTTP 工具时，网关会先同步执行完工具、拿到最终答案，再把结果合成 SSE 回放，保证 `stream: true` 的客户端能正常解析。

**测试（模拟客服）**：点智能体列表里的「测试」直接打开客服对话框，用后台登录态调用，自动带上该智能体的系统 Prompt、插件与知识库，支持图片 / 文本文件上传和多轮上下文，回复下方显示命中的渠道、模型、耗时与知识库命中数，不占用 API Key 的调用额度。在线测试页也能选择智能体，用同一套逻辑试跑。

**网页客服插件**：在智能体列表点「测试 → 生成插件」（或直接在「客服插件」页点「编辑」），配置标题、副标题、欢迎语、提问案例（每行一个）、主题色、弹出位置、使用模型，并选择一个已存在的 API Key。这个 Key **只在服务端**用于扣额度与模型授权。保存后得到两种接入方式：

```html
<!-- 方式一：嵌入客户网页的右下角气泡球 -->
<script src="https://你的网关地址/embed/xxxxxxxx.js" async defer></script>

<!-- 方式二：在线链接，浏览器打开即是完整客服页面 -->
https://你的网关地址/embed/xxxxxxxx
```

两种方式共用同一个地址标识与同一份配置，改完配置保存即生效，客户页面和已发出的链接都不需要更换。

脚本本体、窗口参数与鉴权全部托管在网关：客户页面加载后，云端脚本再拉取标题 / 欢迎语 / 案例 / 主题色等配置并渲染（右下角客服窗口，可收缩成气泡球，支持图片与多轮对话，窄屏自动全屏）。网页源码里没有 API Key，也没有任何插件配置，改配置不需要改客户页面。

**对话走云端代理**（`/embed/api/<id>/chat`）：浏览器只带插件地址，网关按地址找到智能体与关联 Key，固定模型与知识库（工具 = 智能体插件 + 该 Key 关联的插件），调用上游并按关联 Key 扣额度、写日志。插件地址被复制也只能聊这一个客服窗口；生图 / 视频 / 其它模型都调不到。

**客服插件管理**（`/db_admin/widgets`）：列出所有已生成的插件，可重新编辑配置、复制嵌入代码或在线链接、上架 / 下架、重置地址、删除。下架后已嵌入的窗口不再渲染、在线链接提示已下架、对话接口返回 403；重置地址会让所有旧 `<script src>` 与旧在线链接立即失效，需要重新复制。

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
- 请求头 `X-Knowledge-Ids: 1,2`：临时指定知识库（覆盖智能体与 API Key 的绑定）。
- 请求头 `X-Task-Type: ocr`：强制走 OCR 类模型。
- `"task_type": "vision"`：请求体里强制类型。

API Key 绑定了智能体时，以上参数都不需要额外传——Prompt、插件与知识库会按智能体配置自动生效。API Key 直接关联了插件时，插件提示词与工具同样自动生效（与智能体插件合并去重）。

### 5.4 响应头

| 响应头 | 含义 |
| --- | --- |
| `X-Ou-Model` | 实际上游模型名（URL 编码） |
| `X-Ou-Channel` | 实际命中的渠道名（URL 编码） |
| `X-Ou-Type` | 本次调度的能力类型 |
| `X-Ou-Rag-Hits` | 知识库命中条数 |
| `X-Ou-Agent` | 本次生效的智能体名（URL 编码，未绑定时不返回） |
| `X-Ou-Tool-Rounds` | 网关代执行的插件工具轮数（仅 chat/completions） |
| `X-Ou-Latency-Ms` | 总耗时（含故障切换与工具调用） |

响应体的 `model` 字段会被回填为客户端请求的模型名；智能体指定了默认模型时回填为该智能体模型（流式响应除外，流式按上游原样透传）。

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
OupengAI/
├── app.py                     # Flask 应用入口、CORS、模板过滤器
├── config.py                  # 配置与六类能力定义
├── db.py                      # SQLite 建表、连接管理、查询 helper
├── requirements.txt
├── start.bat
├── services/
│   ├── router.py              # 调度核心：候选展开、Key 轮换、熔断、上游请求
│   ├── knowledge.py           # 切片、哈希向量、检索、RAG 注入
│   ├── agents.py              # 智能体 Prompt 组装、工具定义、HTTP 工具执行
│   └── widgets.py             # 网页客服插件：配置、公开 token、自包含 JS 生成
├── routes/
│   ├── openai_api.py          # /v1/* OpenAI 兼容端点（含智能体、工具循环、插件 token）
│   ├── admin_api.py           # /db_admin/api/* 管理接口
│   └── admin_pages.py         # /db_admin/* 页面路由
├── templates/                 # 登录、概览、渠道、调度、知识库、智能体、客服插件、插件、Key、日志、测试、设置
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
`knowledge_bases`、`kb_documents`、`kb_chunks`、`plugins`、`agents`、`agent_plugins`、
`agent_kbs`、`api_keys`（含 `agent_id` 绑定）、`api_key_kbs`、`api_key_plugins`、`call_logs`、`settings`。

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
| `OUPENG_ADMIN_PASSWORD` | `123456` | 首次启动创建超级管理员 `yb_admin` 时使用的初始密码（已有账号不受影响） |
| `OUPENG_CONNECT_TIMEOUT` | `10` | 连接上游超时（秒） |
| `OUPENG_UPSTREAM_TIMEOUT` | `180` | 单次请求超时（渠道可覆盖） |
| `OUPENG_RETRY_PER_CANDIDATE` | `0` | 单个候选内部重试次数 |
| `OUPENG_MAX_REQUEST_BYTES` | 64MB | 请求体上限 |

---

## 9. 测试

```powershell
python -m unittest discover -s tests -t . -v
```

70 项测试，覆盖：

- 鉴权（缺 Key / 错 Key / 停用）
- 文本、图片理解、OCR（提示词与请求头两种判定）、生图、生视频的路由
- 主渠道 500 自动切备用渠道
- 多 Key 轮换与 401 冷却
- 视觉 → OCR 兜底链
- 流式 SSE 透传
- 知识库入库、检索、按 API Key 绑定注入、`X-Knowledge: off`、`knowledge.kb_ids` 指定库
- API Key 关联插件：tools 注入、网关代执行工具并回填、停用插件不注入、增删改与级联清理
- 优先级的排序、加权轮询负载均衡
- 后台全部页面渲染与完整 CRUD 流程（含 Key 脱敏、密码修改）

测试用进程内假上游（`tests/mock_upstream.py`）替代真实 HTTP，因此在无外网/无 localhost 连接的环境也能跑。

---

## 打包为 Windows exe

```powershell
python -m pip install pyinstaller
.\build_exe.ps1
```

产物：`dist\OupengAI.exe`（单文件，约 32MB）。首次运行会在 exe 同目录创建 `data\oupeng.db`，升级 exe 不会丢数据；端口可用 `OUPENG_PORT` 环境变量修改。`templates` 与 `static` 已通过 `--add-data` 内嵌，脚本里排除了 numpy / PIL 等无关依赖以减小体积。

发布时把 `OupengAI.exe` 和 `README-FIRST.txt` 一起压缩即可；单文件 exe 首次启动需要解压，约 3~10 秒，部分杀毒软件可能误报，加入白名单即可。

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

---

## 12. 开源与反馈

- 源码仓库：<https://github.com/sande2018/oupengAI>
- 问题反馈：<https://github.com/sande2018/oupengAI/issues>
- 后台每个页面底部都标注了开源地址；右下角「打赏作者奶茶」鼠标悬停会显示微信赞赏码（`static/8b2df04380d487f6778af3c40593134c.jpg`）。

如果这个项目对你有帮助，欢迎点个 Star。

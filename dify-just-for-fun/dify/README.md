# 谜海拾贝 —— Dify × OceanBase seekdb 智能应用

「谜海拾贝」是一个**会出谜语、陪你猜谜、还能讲冷知识**的好玩智能应用，用于 OceanBase 比赛的 Dify 题目（*Just for Fun*）。

- **Dify**：负责对话编排、意图识别、LLM 生成（使用本地 Ollama `deepseek-r1:8b`）。
- **seekdb**（OceanBase 原生混合搜索数据库）：负责谜语与冷知识的**向量 + 全文混合检索**（RRF 融合），是应用知识底座。
- **桥接服务**（Node.js）：把 seekdb 的检索能力封装成 HTTP API，供 Dify 通过 `HTTP 请求` 节点调用。
- **推理内容剥离**：`deepseek-r1` 为推理模型，其输出会附带 `<think>...</think>` 思考过程（可能提前暴露谜底）。DSL 中每个 LLM 节点后都接了一个「去除思考」代码节点，正则剥离思考块，只保留最终回答；同时模型参数已设 `think: false`。

---

## 一、架构

```
┌─────────┐   用户提问    ┌──────────────────────┐
│  用户   │ ───────────▶ │  Dify Chatflow (Docker) │
└─────────┘               │  · 意图分类            │
                          │  · LLM (Ollama R1)     │
                          └───────────┬────────────┘
                                      │ HTTP 请求 (POST /api/retrieve)
                                      ▼
                          ┌──────────────────────┐
                          │  seekdb 桥接服务 (Node) │  ← 本仓库 dify/bridge
                          └───────────┬────────────┘
                                      │ seekdb SDK (混合检索 + 自动向量化)
                                      ▼
                          ┌──────────────────────┐
                          │  seekdb server (Docker)│  ← oceanbase/seekdb:latest
                          └──────────────────────┘
```

---

## 二、目录结构

```
dify/
├── app.dsl.yml            # Dify 应用 DSL（可直接导入 Dify）
├── README.md              # 本运行说明
├── VALIDATION.md          # 验证样例
└── bridge/
    ├── server.js          # seekdb 桥接服务（HTTP API，端口 3001）
    ├── seed.js            # 数据初始化脚本
    ├── seekdb.js          # seekdb 客户端封装
    └── data.js            # 谜语 + 冷知识数据集
```

---

## 三、前置要求

| 依赖 | 版本 / 说明 |
| --- | --- |
| Docker Desktop | 运行 seekdb 与 Dify |
| Node.js | ≥ 20（本机已验证 22.x） |
| Dify | 社区版，Docker 部署 |
| Ollama（本地） | 已部署 `deepseek-r1:8b`，用于 LLM 生成 |

---

## 四、启动步骤

### 1. 启动 seekdb

在仓库根目录执行：

```bash
docker compose up -d
```

等待 seekdb 就绪（约 15~30 秒），可验证：

```bash
docker exec stepfun-seekdb mysql -uroot -h127.0.0.1 -P2881 -e "SELECT 1"
```

### 2. 安装依赖并灌数据

```bash
npm install
npm run seed
```

`npm run seed` 会把 24 条谜语 + 20 条冷知识写入 seekdb 集合 `stepfun_riddles`，并自动向量化（使用 seekdb 默认 embedding `all-MiniLM-L6-v2`，384 维）。

### 3. 启动桥接服务

```bash
npm run bridge
```

看到以下输出即成功：

```
  seekdb 桥接服务（Dify 专用）
  - 检索: POST http://localhost:3001/api/retrieve
  ...
[seekdb] ✓ 已连接 127.0.0.1:2881
```

> 桥接服务默认监听 `3001` 端口，可通过环境变量 `BRIDGE_PORT` 修改。

### 4. 部署 Dify 并导入应用

```bash
git clone https://github.com/langgenius/dify.git
cd dify/docker
cp .env.example .env
docker compose up -d
```

浏览器打开 `http://localhost`，完成初始化后：

1. **配置模型**：`设置 → 模型供应商 → Ollama`，Base URL 填 `http://host.docker.internal:11434`，添加模型 `deepseek-r1:8b`。
2. **导入应用**：`创建应用 → 从 DSL 文件导入`，选择本仓库的 `dify/app.dsl.yml`。
3. **确认 HTTP 节点地址**：打开应用工作流，检查两个「检索」节点（`检索谜语`、`检索冷知识`）的 URL 为：
   - Windows/macOS：`http://host.docker.internal:3001/api/retrieve`
   - Linux：请改用宿主机局域网 IP，例如 `http://192.168.1.100:3001/api/retrieve`
4. **发布**并开始对话。

---

## 五、桥接 API 说明

### `POST /api/retrieve`

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 查询文本 |
| `category` | string | 否 | `riddle`（谜语）/ `fact`（冷知识）/ `all`（默认） |
| `n` | number | 否 | 返回条数，默认 5 |

返回示例：

```json
{
  "success": true,
  "mode": "hybrid",
  "results": [
    {
      "id": "r013",
      "document": "身披黑白衣，样子真神气……（分类：动物）",
      "metadata": { "type": "riddle", "answer": "大熊猫", "category": "动物", "difficulty": 1 }
    }
  ]
}
```

### 其它端点

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/health` | GET | 健康检查 + seekdb 连接状态 |
| `/api/seed` | POST | 灌入种子数据 |
| `/api/reset` | POST | 清空集合 |
| `/api/stats` | GET | 集合统计 |

---

## 六、提交产物对照

| 题目要求 | 对应产物 |
| --- | --- |
| Dify DSL | `dify/app.dsl.yml` |
| 运行说明 | 本 `README.md` |
| 验证样例 | `dify/VALIDATION.md` |
| 数据与初始化方式 | `dify/bridge/data.js` + `npm run seed` |
| 配套源码及启动说明 | `dify/bridge/` + 本 README |

---

## 七、常见问题

- **seekdb 连接失败**：确认 `docker compose up -d` 已执行且容器状态为 healthy。
- **Dify 里 HTTP 节点报连接超时**：检查 URL 是否用对了 `host.docker.internal`（Linux 需改宿主机 IP）。
- **首次 `npm run seed` 较慢**：首次运行需下载 embedding 模型（走 hf-mirror 镜像，仅第一次）。
- **DSL 导入报错 / 版本不兼容**：不同 Dify 版本的 DSL 字段可能有差异。可直接在 Dify 控制台新建「Chatflow」应用，按本 README 第六节的功能描述手动搭建：开始 → 意图分类（5 类）→ 两个「HTTP 请求」节点（检索谜语/冷知识，URL 指向桥接服务）→ 相应 LLM 节点与「会话变量」`riddle_context`。节点与连线关系见 `app.dsl.yml` 的 `graph` 段。

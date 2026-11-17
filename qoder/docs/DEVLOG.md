# Qoder 开发记录

项目的主要开发工作在本机 Qoder CN 中完成，包含需求拆解、接口核对、编码、联调、UI 验证与文档整理。本记录按时间顺序整理实际发生的工作。

## 1. 需求拆解

Qoder 先读取赛题文档（Qoder 赛道题面：Build with Qoder），提取约束并形成开发清单：

- 必须使用 Qoder 开发，并用 Qoder Sites 发布项目网站
- 必须实际使用 seekdb 或 PowerContext 中的至少一种承担核心功能
- 资产放入 `2026-competition-ecosystem` 分支的 `qoder/` 目录
- 提交物：项目网站、代码仓库、运行说明、验证样例、Qoder 开发记录

选题落在题面「学习助手」方向：数据结构复习教练，把薄弱点、进度、偏好交给 PowerContext，实现跨会话复用。选择 PowerContext 而非 seekdb 的原因：复习场景的核心诉求是「记住人」，PowerContext 的 Memory API 直接对应，而且本地 Server 无需模型 Key 即可演示。

## 2. 技术核对

Qoder 读取 PowerContext 1.2.0 的安装文档与 OpenAPI（`openapi/powercontext.yaml`），确认：

- 本地 `powercontext server run` 默认监听 `127.0.0.1:8000`，SQLite 持久化，默认开放访问
- Scope 创建：`POST /v1/scopes`；默认 Scope：`GET /v1/scopes/default`
- 记忆写入：`POST /v1/memory/remember`（`scope_id`、`kind`、`text`）
- 记忆检索：`POST /v1/memory/search`（FTS 在无模型时可用）
- 记忆列表：`POST /v1/memory/entries/list`

并用 curl 对真实 Server 做了写入 + 检索冒烟测试，确认 `kind` 与中文文本均可用。

## 3. 实现

Qoder 生成并迭代了以下模块：

| 模块 | 内容 |
| --- | --- |
| `powercontext_client.py` | Scope 查找/创建、remember、search、entries/list、健康检查 |
| `planner.py` | 从 `[主题]` 前缀的记忆中统计薄弱点，按「难度 + 2×薄弱次数」排题 |
| `app.py` | FastAPI 路由：bootstrap、开始复习、提交、记忆库、健康检查 |
| `static/` | 单页前端：召回面板、答题、结果、记忆库 |
| `data/questions.json` | 14 道数据结构题（线性表、栈与队列、树、图、排序、哈希、查找） |
| `scripts/verify_demo.py` | 两轮复习的端到端验证 |

记忆模型设计：

- 每个学习者一个 Scope（`StudyLoop · <名字>`，创建时带幂等键）
- `weakness`：`[主题] <题号> 错题：...`，按题号去重
- `progress`：每轮结束写入时间、题数、正确数与待加强主题
- `preference`：勾选「只复习薄弱点」时写入，下次自动恢复

## 4. 联调与修复

1. **Windows 环境安装**：`uv tool install --force "powercontext[cli,server]==1.2.0"` 安装成功；`powercontext server run` 正常启动，`/health/ready` 返回 ready。
2. **httpx 代理解析失败**：本机 `NO_PROXY` 含 `[::1]`，httpx 0.28 初始化 Client 时抛 `InvalidURL: Invalid port ':1]'`。Qoder 定位到 `NO_PROXY` 解析问题，在客户端统一加 `trust_env=False`（只访问本机，无需代理），应用恢复正常。
3. **接口联调**：通过 `/api/session/start` 与 `/api/session/submit` 完整跑通一轮，确认薄弱点、进度写入，并在下一轮召回。
4. **验证脚本**：运行 `scripts/verify_demo.py`，5 项检查全部通过（错题产生、写入、召回、进入排题依据、薄弱主题优先）。
5. **UI 验证**：在浏览器中实测首页、两轮复习、结果页与记忆库；截图见 `docs/screenshots/`。期间修正了结果页选项显示（改为 A/B/C/D）并在提交后隐藏过期的召回面板。

## 5. 站点发布

项目展示网站使用 Qoder Sites 创建并发布，包含项目简介、技术分工、运行说明、截图与仓库链接：

- 发布地址：<https://studyloop-7cdph2t6uli.qoder.zone>
- 访问范围：任何人都可访问（已按赛题要求在 Qoder Sites 中设为公开，并用未登录请求确认返回 200）

代码仓库位于团队仓库 `FRabandoning/PowerContext_problem` 的 `qoder-studyloop` 分支（基于 `2026-competition-ecosystem` 创建），资产在 `qoder/` 目录。

## 6. 说明

- 开发过程中的命令执行、接口验证、浏览器实测均在 Qoder 会话内完成，可通过提交历史与截图核对。
- 项目未保存本地学习状态，关闭 PowerContext 后记忆不可见，用于证明记忆确实来自 PowerContext。
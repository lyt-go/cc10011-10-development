# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 异常检测的数据流

异常检测比其他模块多一条内部数据流，跑在 `app/services/anomalydetect.py`：

```text
监控指标时序 + 检测模型参数
  → 提交训练：读取实时指标定义确定时序口径，产出模型版本（内容指纹）
  → 部署模型：上线前检查训练环境与模型依赖一致，生成部署快照（部署版本）
  → 生成事件数据集：按部署快照算异常结果，一个业务批次一份
  → 异常事件：逐条绑定模型版本、部署版本、训练窗口
```

几个刻意定下来的规则：

- **版本指纹**：模型版本 = 编号 + 监控指标 + 参数 + 训练窗口的稳定指纹；部署版本
  = 模型版本 + 训练窗口 + 参数 + 环境指纹。同一次训练重复部署得到同一部署版本，
  参数、窗口或运行时变了就是新的部署版本，页面上的异常卡片可以顺着它回到那次训练。
- **版本冲突的取舍**：同一模型编号同一时刻只允许一个「生效中」部署快照，冲突时
  **先到先得**——旧版本不被挤掉，新版本必须等旧版本停用或回训收口后才能上线。
  这样每条异常事件引用到的部署版本，在写入当时都是唯一生效的，不存在两条并存版本
  各产一份事件；历史事件保留旧版本号，回训边界因此可追溯。
- **回训**：已部署模型可以再次提交训练。训练时旧快照转入「已停用」并记录停用原因
  （回训、截止日期），之后生成的事件只会引用新部署版本。
- **上线前检查**：训练记录里存了训练运行时、模型运行时、指标时区与环境指纹；
  部署时逐项比对当前配置（`app/config.py`），不一致就拒绝上线并列出差异项。
- **业务批次幂等**：同一业务批次并发到达只在锁内落一份数据集，重复请求返回既有数据集；
  数据集状态只能沿「待训练 → 模型训练中 → 快照已生成 → 已完成」向前推进，
  中途失败整批回滚，不留半成品。
- **共用事件记录**：告警历史页与运营概览读的是同一份异常事件
  （`/api/alarmhistory/anomaly-events`、`/api/anomalydetect/events`），不另存副本。

相关接口：`GET /api/anomalydetect/snapshots`、`GET /api/anomalydetect/datasets`、
`POST /api/anomalydetect/datasets`、`GET /api/anomalydetect/events`。

领域：数据工程
子方向：数据分析与数据应用

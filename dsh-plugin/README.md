# dsh-pet-pyside6

PySide6 桌面宠物（[pet-pyside6](../../)）的 DeepSeek Harness 插件。

它是 pet-pyside6 与 DSH 之间的桥：

1. **单实例拉起** —— 先探测桌宠自带的 `GET /state`，已在运行就复用，绝不重复启动；
2. **事件转发** —— 把 DSH 的 `session/event` 转发到桌宠的 `POST /event`，驱动工作状态动画；
3. **余额推送** —— 复用 dsh-dafeiyu 的余额实现（api-key / account 两路），并对外提供
   `GET/POST /plugins/dsh-pet-pyside6/balance`。

## 安装

在本机 DSH profile 里安装（本地目录，不改 DSH 自身的 profile 文件）：

```
plugin_manager(action: "install_bundle", target: "<绝对路径>/pet-pyside6/dsh-plugin")
```

安装器会写入 profile 的 `dependencies`、`dsh.profile.bundles`，并建立指向源码目录的 Junction。
改动源码后需要**完全重启 DSH**（Host 侧）或刷新页面（Client 侧）。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 启用插件 |
| `autoStart` | `true` | 未见运行实例时自动拉起桌宠 |
| `projectDir` | 包的上一级 | 含 `main.py` 的工程目录 |
| `pythonPath` | `""` | 留空则使用 PATH 里的 `python` |
| `port` | `3080` | 桌宠本地 HTTP 端口 |
| `forwardEvents` | `true` | 转发 DSH 会话事件 |
| `balanceEnabled` | `true` | 查询并推送余额 |
| `balanceSource` | `auto` | `auto` / `api-key` / `account` |

工程目录默认由包位置推导（`dsh-plugin/../..`），也可用环境变量 `DSH_PET_PYSIDE6_DIR` 覆盖。

## 依赖说明（重要）

`src/plugin.js` 用 `@deepseek-ai/schemastery` 定义 Config schema。本包以 **link** 方式安装时，
Node 会从源码目录向上查找 `node_modules`，找不到 profile 里的副本。因此：

- 仓库**不提交** `node_modules/`（见 `.gitignore`）；
- 缺失时插件会降级为"无 Config schema"，**功能照常**；
- 需要设置页表单时，把下面三个目录复制到 `dsh-plugin/node_modules/`：

```
@deepseek-ai/schemastery
@deepseek-ai/cosmokit
@standard-schema/spec
```

来源可以是 `~/.dsh/profiles/desktop/node_modules/` 或任意 `dsh-dafeiyu` 安装目录。

## 本地路由

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/plugins/dsh-pet-pyside6/balance` | 读缓存的余额快照 |
| POST | 同上 | 强制刷新一次 |

`src/local-http.js` 会校验 `remoteAddress`、`Host`、`Origin`，仅接受本机同源请求。

## 目录

```
dsh-plugin/
├─ package.json          # dsh.bundle.patch + dsh.client
├─ cordis.patch.yml      # 插入 dsh-pet-pyside6 行
├─ icon.svg              # 插件栏图标
├─ locale/{zh,en}.json   # meta.title / meta.description
├─ src/
│  ├─ index.js           # 入口，动态 import + 失败降级
│  ├─ plugin.js          # 拉起/复用、事件转发、余额
│  ├─ balance.js         # 余额实现
│  └─ local-http.js      # 本机请求校验
└─ lib/client.js         # 浏览器侧卡片（window.__ModuleLoader__）
```

## 许可

MIT

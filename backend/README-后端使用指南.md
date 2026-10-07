# 凡凡选股 · 后端使用指南

后端 = `backend/` 目录里的 Python 脚本。它的作用有两个：

1. **AI 复盘**（`fanfan_review.py`）：调用 AI 生成当日复盘报告
2. **全局缓存防封**（`fanfan_cache/snapshot/scanner/server.py`）：服务器统一抓取东财数据，你多个设备只读缓存，防止 IP 被封

> ⚠️ 后端**不能**跑在 GitHub Pages 上（GitHub Pages 只托管静态文件）。它需要跑在**你自己的电脑**（Windows/Mac/Linux）或**云服务器**上。

---

## 一、后端能给你什么

| 功能 | 说明 | 需要 |
|---|---|---|
| AI复盘 | 每日复盘：市场综述、主线板块、龙虎榜资金、次日预期、操作建议 | 需要 AI API Key |
| 全局缓存 | 服务器每30秒抓一次全市场数据，你手机/电脑只读缓存，不直接碰东财接口 | 不需要 Key |
| 静态托管 | 后端自带网页服务，跑起来后手机直接访问，不用 GitHub | 不需要 Key |

---

## 二、环境要求

- Python 3.8+（Windows 去 python.org 安装，Mac 自带，Linux 用 `apt install python3`）
- 全部是 Python 标准库，**不需要安装任何第三方库**（AI 调用用内置 urllib）
- 一台能常开的电脑（或云服务器）

---

## 三、最快上手（本地电脑跑）

### 第 1 步：准备文件

把 `backend/` 整个文件夹复制到电脑上（比如桌面），例如 `D:\fanfan\` 或 `~/fanfan/`。

```
backend/
├── fanfan_cache.py      ← 缓存核心（数据库）
├── fanfan_snapshot.py   ← 盘中快照（每30秒抓全市场）
├── fanfan_scanner.py    ← 盘后深度计算（K线/评分）
├── fanfan_server.py     ← API服务器（前端访问入口）
├── fanfan_review.py     ← AI复盘
├── fanfan_yijiner.py    ← 一进二计算
├── auction_generator.py ← 竞价数据生成
├── start.sh / stop.sh   ← 一键启动/停止（Mac/Linux）
```

### 第 2 步：启动（Mac / Linux）

```bash
cd ~/fanfan
./start.sh
```

启动后你会看到：
- 📊 盘中快照抓取器（后台运行，每30秒抓一次）
- 🚀 API 服务器（前台，端口 8080）

### 第 2 步（Windows）

Windows 没有 `.sh`，用两个命令行分别跑：

```bat
:: 窗口1：先抓一次数据（否则缓存是空的）
python fanfan_snapshot.py --once --force

:: 窗口2：启动 API 服务器
python fanfan_server.py --port 8080
```

### 第 3 步：访问

浏览器打开（本机）：
```
http://localhost:8080/
```

手机访问（同一个 WiFi，把 localhost 换成电脑的局域网 IP）：
```
http://192.168.x.x:8080/
```
> 电脑 IP 查看：Windows `ipconfig`，Mac `ipconfig getifaddr en0`，Linux `hostname -I`

> ⚠️ Windows 首次可能弹防火墙提示，点"允许访问"。

---

## 四、配置 AI 复盘（可选但有它才完整）

AI 复盘默认走 OpenAI 兼容接口，国内推荐用 DeepSeek（便宜好用）。

### 方式 A：命令行设置环境变量（临时）

```bash
# DeepSeek 示例
export FANFAN_AI_API_KEY="sk-你的DeepSeek密钥"
export FANFAN_AI_BASE_URL="https://api.deepseek.com/v1"
export FANFAN_AI_MODEL="deepseek-chat"

# 然后启动
./start.sh
```

Windows（PowerShell）：
```powershell
$env:FANFAN_AI_API_KEY="sk-你的DeepSeek密钥"
$env:FANFAN_AI_BASE_URL="https://api.deepseek.com/v1"
$env:FANFAN_AI_MODEL="deepseek-chat"
```

### 方式 B：直接改代码（一劳永逸）

打开 `fanfan_review.py`，第 30-33 行：

```python
AI_API_KEY = os.environ.get("FANFAN_AI_API_KEY", "")   # 改成 "sk-你的密钥"
AI_BASE_URL = os.environ.get("FANFAN_AI_BASE_URL", "https://api.openai.com/v1")  # 改成 DeepSeek 地址
AI_MODEL = os.environ.get("FANFAN_AI_MODEL", "gpt-4o-mini")  # 改成 deepseek-chat
```

### 不配 Key 也能用

网页里 AI 复盘 Tab 有"演示模式"按钮，不调用 AI、生成模板示例；后端也支持 `--demo` 参数：
```bash
python3 fanfan_review.py --demo
```

---

## 五、各脚本单独怎么跑（进阶）

| 想干什么 | 命令 |
|---|---|
| 先抓一次全市场数据（测试） | `python3 fanfan_snapshot.py --once --force` |
| 盘中持续抓（每30秒） | `python3 fanfan_snapshot.py` |
| 盘后深度计算（起爆前夜/情绪周期） | `python3 fanfan_scanner.py --type all` |
| 只算 K 线 | `python3 fanfan_scanner.py --type kline` |
| 生成今日 AI 复盘 | `python3 fanfan_review.py` |
| 生成指定日期复盘 | `python3 fanfan_review.py --date 2026-09-16` |
| 强制重生成复盘 | `python3 fanfan_review.py --force` |
| 一进二候选 | `python3 fanfan_yijiner.py --date 2026-09-16 --top 3` |
| 停止所有后台任务 | `./stop.sh` |

**推荐顺序（一天流程）：**

```bash
# 早晨/盘中：抓取器挂着
./start.sh

# 收盘后（15:30 以后）：深度计算 + 复盘
python3 fanfan_scanner.py --type all
python3 fanfan_review.py --date 今天日期
```

---

## 六、部署到云服务器（7×24小时在线，可选）

想让后端 7×24 小时跑、手机随时随地访问，需要一台云服务器（阿里云/腾讯云轻量服务器，约 10-50 元/月）：

```bash
# 服务器上（Ubuntu）
sudo apt update && sudo apt install -y python3
mkdir ~/fanfan && cd ~/fanfan
# 上传 backend/ 里的文件到这里
./start.sh
```

然后用服务器公网 IP 访问：`http://服务器IP:8080/`

> ⚠️ 记得在云厂商控制台"安全组"里**放行 8080 端口**，否则外部访问不了。

---

## 七、常见问题

| 问题 | 原因 | 解决 |
|---|---|---|
| 打开页面显示"无数据/缓存为空" | 还没抓过数据 | 先跑 `python3 fanfan_snapshot.py --once --force` |
| 复盘一直"生成中" | 没配 API Key 或 Key 无效 | 检查环境变量，换 `--demo` 先试 |
| 手机访问不了 | 防火墙/不在同一网络 | 关防火墙、确认同一 WiFi、用 IP 而非 localhost |
| 端口被占用 | 8080 被其他程序用了 | `python3 fanfan_server.py --port 8081` |
| 后台抓取停了 | 电脑休眠/关终端 | 保持电脑开机；云服务器则无此问题 |

---

## 八、重要提醒

- ⚠️ 本工具仅为数据整理参考，**不构成任何投资建议**，股市有风险，交易需谨慎。
- 行情数据来自公开接口，可能存在延迟，以交易所披露为准。
- 请勿将本工具用于任何违法用途。

# 深圳青年驿站自动化监控脚本 v2.0

自动监控 [深圳青年驿站](https://home.szyouth.cn/application) 的床位可预订情况，当发现连续 N 天可预订时通过企业微信 Webhook 发送通知。

## 功能特点

- **多站点监控**: 支持同时监控全部 41 个活跃站点，或指定多个/单个站点
- **精准数据**: 使用 `/day30` API 获取真实的 30 天可预订数据（而非 Detail API 的静态床位数）
- **反检测机制**: UA 轮换、随机延迟、真实浏览器请求头
- **企业微信通知**: 发现连续可预订区间时自动推送，支持去重和频率控制
- **Webhook 智能验证**: 自动识别 UUID Key 或完整 URL，启动时测试连通性
- **优雅退出**: 支持 Ctrl+C 安全退出

## 环境要求

- Python 3.7+
- requests 库

```bash
pip install requests
```

## 快速开始

### 1. 查看所有驿站

```bash
python szyouth_monitor.py --list-stations
```

### 2. 监控全部站点（推荐）

```bash
python szyouth_monitor.py -x 30 -y 7 -s all -w YOUR_WEBHOOK_KEY
```

### 3. 监控指定站点

```bash
# 监控多个站点
python szyouth_monitor.py -x 15 -y 5 -s 75,85,86 -w YOUR_KEY

# 监控单个站点
python szyouth_monitor.py -x 10 -y 3 -s 85 -w YOUR_KEY
```

### 4. 仅运行一次（测试用）

```bash
python szyouth_monitor.py -x 10 -y 3 -s 85 --once
```

## 参数说明

| 参数 | 缩写 | 必需 | 说明 |
|------|------|------|------|
| `--interval` | `-x` | ✅ | 查询间隔（分钟），最小 10 分钟 |
| `--days` | `-y` | ✅ | 连续可预订天数阈值 |
| `--station` | `-s` | ✅ | 驿站ID: `all` / `75,85,86` / `85` |
| `--webhook-key` | `-w` | ❌ | 企业微信 Webhook Key 或完整 URL |
| `--list-stations` | | ❌ | 列出所有驿站信息 |
| `--once` | | ❌ | 只检查一次后退出 |

## 企业微信 Webhook 配置

### 获取 Webhook Key

1. 打开**企业微信桌面端**
2. 进入需要接收通知的**群聊**
3. 点击右上角 **`...`** → **群机器人** → **添加** → **新建机器人**
4. 输入机器人名称（如 "驿站监控"），创建后会得到 Webhook 地址：
   ```
   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ```
5. 复制 `key=` 后面的 UUID 部分作为 `-w` 参数值

### 使用方式（三选一）

**方式一: 命令行参数（直接传 Key）**
```bash
python szyouth_monitor.py -x 30 -y 7 -s all -w xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

**方式二: 命令行参数（传完整 URL，脚本自动提取 Key）**
```bash
python szyouth_monitor.py -x 30 -y 7 -s all -w "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

**方式三: 环境变量**
```bash
# Windows
set SZYOUTH_WEBHOOK_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
python szyouth_monitor.py -x 30 -y 7 -s all

# Linux/Mac
export SZYOUTH_WEBHOOK_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
python szyouth_monitor.py -x 30 -y 7 -s all
```

### 常见错误

| errcode | 含义 | 解决方法 |
|---------|------|----------|
| 93000 | invalid webhook url | Key 格式不对。确认使用的是 UUID 格式（如 `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`），不要传入完整 URL 中非 Key 的部分 |
| 0 | 成功 | 正常 |

> **脚本会在启动时自动测试 Webhook 连通性**，如果 Key 无效会立即报错退出，无需等到检测到可预订房间才发现配置错误。

## 反检测机制

为避免被服务器识别为爬虫，脚本内置以下防护措施：

- **User-Agent 轮换**: 每次重试随机使用不同的浏览器 UA
- **真实请求头**: 包含 Referer、Origin、Sec-Fetch-* 等浏览器标准头
- **随机延迟**: 站点间请求间隔 1.5~3.0 秒，5% 概率触发 3~8 秒的"思考暂停"
- **最小间隔**: 监控轮次间隔至少 10 分钟
- **Session 复用**: 使用 requests.Session 保持连接，模拟真实浏览行为

## API 说明

### v2.0 使用的正确 API

```
GET /api/hotels/{id}/day30?user_gender=1000
```

返回格式：
```json
{
  "status": 200,
  "data": {
    "2026-04-13": 0,
    "2026-04-17": 1,
    "2026-04-19": 1,
    "2026-04-25": 1
  }
}
```

- 值为 `0` = 已约满
- 值 > `0` = 可预订（数值为可用床位数）
- `user_gender=1000` 是唯一返回真实可预订数据的参数值

### v1.0 使用的错误 API（已弃用）

```
GET /api/hotels/{id}?come_date=X&leave_date=Y
```

该接口返回的 `male_beds_count` / `female_beds_count` 是**静态容量**而非实时可预订数，导致即使日历显示"已约满"也会被误判为有房——这是 v1.0 误报的根本原因。

## 日志

- 控制台输出: INFO 级别
- 日志文件: `szyouth_monitor.log`（自动轮换，最大 5MB × 3 份）

## 示例输出

```
2026-04-12 10:00:00 [INFO] =======================================================
2026-04-12 10:00:00 [INFO] 开始第 1 轮检查 - 2026-04-12 10:00:00
2026-04-12 10:00:00 [INFO] 监控 41 个站点 | 连续天数阈值: ≥7 天
2026-04-12 10:00:01 [INFO] [1/41] 查询: 粤港澳大湾区（宝安）青年驿站 (ID:75)
2026-04-12 10:00:01 [INFO]   📊 30天中有 8/30 天可预订
2026-04-12 10:00:01 [INFO]   ❌ 无连续 ≥7 天区间
2026-04-12 10:00:04 [INFO] [2/41] 查询: 青年驿站（龙岗龙城CC公寓站） (ID:85)
2026-04-12 10:00:04 [INFO]   📊 30天中有 12/30 天可预订
2026-04-12 10:00:04 [INFO]   🎉 2026-04-17 ~ 2026-04-25 (9天连续可预订)
...
```

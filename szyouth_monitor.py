#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
深圳青年驿站自动化监控脚本 v2.0
使用 /day30 API 获取准确的30天可预订数据，支持多站点同时监控。
通过企业微信 Webhook 发送通知。

用法示例:
    python szyouth_monitor.py -x 30 -y 7 -s all -w YOUR_WEBHOOK_KEY
    python szyouth_monitor.py -x 15 -y 5 -s 75,85,86 -w YOUR_KEY
    python szyouth_monitor.py -x 10 -y 3 -s 85 --once
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import requests

# ============ 常量 ============
API_BASE_URL = "https://api-home.szyouth.cn/api"
WECHAT_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # 指数退避基数(秒)
MIN_INTERVAL_MINUTES = 10  # 最小监控间隔(分钟)

# 反检测: User-Agent 池
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# ============ 日志配置 ============
def setup_logger():
    logger = logging.getLogger("SzyouthMonitor")
    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(console)

    try:
        fh = RotatingFileHandler(
            "szyouth_monitor.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
        ))
        logger.addHandler(fh)
    except Exception:
        logger.warning("无法创建日志文件，仅使用控制台输出")

    return logger


log = setup_logger()


# ============ HTTP 客户端 (反检测) ============
class APIClient:
    """带反检测机制和重试逻辑的 HTTP 客户端"""

    def __init__(self):
        self.session = requests.Session()
        self._rotate_headers()

    def _rotate_headers(self):
        self.session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://home.szyouth.cn/",
            "Origin": "https://home.szyouth.cn",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        })

    def smart_delay(self, min_sec=1.5, max_sec=3.0):
        """带随机浮动的请求延迟，5%概率触发较长的思考暂停"""
        delay = random.uniform(min_sec, max_sec)
        if random.random() < 0.05:
            delay += random.uniform(3.0, 8.0)
        time.sleep(delay)

    def get(self, endpoint, params=None):
        """带重试的 API GET 请求"""
        url = f"{API_BASE_URL}{endpoint}"
        for attempt in range(MAX_RETRIES):
            try:
                if attempt > 0:
                    self._rotate_headers()
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 429:
                    wait = RETRY_BACKOFF ** (attempt + 2)
                    log.warning(f"API限流(429)，等待{wait}秒后重试...")
                    time.sleep(wait)
                    continue
                if resp.status_code == 403:
                    log.warning("API拒绝访问(403)，轮换请求头后重试...")
                    self._rotate_headers()
                    time.sleep(RETRY_BACKOFF ** (attempt + 2))
                    continue
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") != 200:
                    log.error(f"API业务错误: {data.get('message')}")
                    return None
                return data.get("data")
            except requests.exceptions.Timeout:
                log.warning(f"请求超时 (尝试 {attempt + 1}/{MAX_RETRIES})")
            except requests.exceptions.ConnectionError as e:
                log.warning(f"连接错误: {e} (尝试 {attempt + 1}/{MAX_RETRIES})")
            except requests.exceptions.RequestException as e:
                log.error(f"请求异常: {e}")
                return None
            except json.JSONDecodeError:
                log.error("API返回非JSON数据")
                return None
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF ** (attempt + 1))
        log.error(f"API请求失败(已重试{MAX_RETRIES}次): {url}")
        return None


# ============ 站点管理 ============
def fetch_all_stations(client):
    """分页获取所有驿站列表"""
    all_stations = []
    page = 1
    while True:
        data = client.get("/hotels", {"page": page})
        if not data or "data" not in data:
            break
        all_stations.extend(data["data"])
        if page >= data.get("last_page", 1):
            break
        page += 1
        client.smart_delay(0.5, 1.5)
    return all_stations


def resolve_station_ids(station_arg, client):
    """
    解析 -s 参数:
      "all"       -> 所有启用状态(status=1000)的站点
      "75,85,86"  -> 指定多个站点
      "85"        -> 单个站点
    返回: [(id, name), ...] 或 None(失败)
    """
    if station_arg.lower() == "all":
        stations = fetch_all_stations(client)
        if not stations:
            log.error("获取站点列表失败")
            return None
        active = [(s["id"], s["name"]) for s in stations if s.get("status") == 1000]
        log.info(f"已获取 {len(active)} 个活跃站点 (共 {len(stations)} 个)")
        return active

    try:
        ids = [int(x.strip()) for x in station_arg.split(",")]
    except ValueError:
        log.error(f"无效的站点ID格式: {station_arg}")
        return None

    # 获取站点名称
    stations = fetch_all_stations(client)
    station_map = {s["id"]: s["name"] for s in stations} if stations else {}
    result = []
    for sid in ids:
        name = station_map.get(sid, f"未知站点({sid})")
        result.append((sid, name))
    return result


# ============ 核心逻辑 ============
def fetch_day30(client, hotel_id):
    """
    调用 /hotels/{id}/day30?user_gender=1000 获取30天真实可预订数据。
    返回: {date_str: bed_count} 如 {"2026-04-17": 1, "2026-04-19": 0, ...}
    注意: 必须使用 user_gender=1000，其他值返回的是总容量而非实际可预订数。
    """
    return client.get(f"/hotels/{hotel_id}/day30", {"user_gender": 1000})


def find_consecutive_available(day30_data, min_days):
    """
    在 day30 数据中寻找连续 >= min_days 天有床位的区间。
    day30_data: {date_str: bed_count}
    返回: [(start_date_str, end_date_str, days_count), ...]
    """
    if not day30_data:
        return []

    sorted_dates = sorted(day30_data.keys())
    results = []
    start = None
    count = 0
    prev_date = None

    for d in sorted_dates:
        curr_date = datetime.strptime(d, "%Y-%m-%d").date()
        try:
            beds = int(day30_data[d])
        except (ValueError, TypeError):
            beds = 0

        if beds > 0:
            if start is None:
                start = d
                count = 1
            elif prev_date and (curr_date - prev_date).days == 1:
                count += 1
            else:
                if count >= min_days:
                    end = prev_date.strftime("%Y-%m-%d")
                    results.append((start, end, count))
                start = d
                count = 1
        else:
            if count >= min_days and start:
                end = prev_date.strftime("%Y-%m-%d")
                results.append((start, end, count))
            start = None
            count = 0
        prev_date = curr_date

    if count >= min_days and start:
        end = prev_date.strftime("%Y-%m-%d")
        results.append((start, end, count))

    return results


# ============ 通知 ============
class NotificationManager:
    """管理通知去重和频率控制"""

    def __init__(self, webhook_key, max_per_hour=20):
        self.webhook_key = webhook_key
        self.max_per_hour = max_per_hour
        self._sent_hashes = {}  # {hash: timestamp}
        self._hourly_count = []  # [timestamp, ...]

    def _make_hash(self, hotel_id, start_date, end_date):
        raw = f"{hotel_id}:{start_date}:{end_date}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _cleanup(self):
        now = time.time()
        self._sent_hashes = {
            h: t for h, t in self._sent_hashes.items() if now - t < 86400
        }
        self._hourly_count = [t for t in self._hourly_count if now - t < 3600]

    def should_notify(self, hotel_id, start_date, end_date):
        self._cleanup()
        h = self._make_hash(hotel_id, start_date, end_date)
        if h in self._sent_hashes:
            log.debug(f"通知已发送过(24h内去重): {hotel_id} {start_date}~{end_date}")
            return False
        if len(self._hourly_count) >= self.max_per_hour:
            log.warning(f"每小时通知上限({self.max_per_hour})已达到，跳过")
            return False
        return True

    def mark_sent(self, hotel_id, start_date, end_date):
        h = self._make_hash(hotel_id, start_date, end_date)
        now = time.time()
        self._sent_hashes[h] = now
        self._hourly_count.append(now)

    def send(self, hotel_id, hotel_name, results):
        """
        发送合并通知（同一站点的所有连续区间合并为一条消息）。
        results: [(start, end, days), ...]
        """
        if not results:
            return False

        new_results = [r for r in results if self.should_notify(hotel_id, r[0], r[1])]
        if not new_results:
            return False

        lines = []
        for start, end, days in new_results:
            lines.append(f"  📅 {start} ~ {end} ({days}天)")

        content = (
            f"## 🎉 青年驿站有连续可预订房间！\n\n"
            f"**驿站**: {hotel_name} (ID: {hotel_id})\n"
            f"**发现 {len(new_results)} 个可预订区间**:\n"
            + "\n".join(lines) + "\n\n"
            f"[点击查看详情](https://home.szyouth.cn/application/{hotel_id})\n\n"
            f"_监控时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"
        )

        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        url = f"{WECHAT_WEBHOOK_URL}?key={self.webhook_key}"

        try:
            resp = requests.post(url, json=payload, timeout=10)
            result = resp.json()
            if result.get("errcode") == 0:
                for start, end, days in new_results:
                    self.mark_sent(hotel_id, start, end)
                log.info(f"✅ 企业微信通知已发送: {hotel_name}")
                return True
            else:
                errcode = result.get("errcode")
                errmsg = result.get("errmsg", "")
                log.error(f"企业微信发送失败: errcode={errcode} errmsg={errmsg}")
                if errcode == 93000:
                    log.error("❌ Webhook Key 无效！请检查配置，详见 README.md 中的 Webhook 配置说明")
                return False
        except Exception as e:
            log.error(f"企业微信通知异常: {e}")
            return False

    def test_webhook(self):
        """启动时测试 Webhook 连通性"""
        payload = {
            "msgtype": "text",
            "text": {"content": "🔔 青年驿站监控脚本已启动，Webhook 连接正常。"}
        }
        url = f"{WECHAT_WEBHOOK_URL}?key={self.webhook_key}"
        try:
            resp = requests.post(url, json=payload, timeout=10)
            result = resp.json()
            if result.get("errcode") == 0:
                log.info("✅ Webhook 连接测试通过")
                return True
            else:
                errcode = result.get("errcode")
                errmsg = result.get("errmsg", "")
                log.error(f"❌ Webhook 连接测试失败: errcode={errcode} errmsg={errmsg}")
                if errcode == 93000:
                    log.error("Webhook Key 无效！请使用正确的 Key（UUID格式，不是完整URL）")
                    log.error("正确格式示例: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
                    log.error("从完整 Webhook URL 中 key= 后面的部分提取")
                return False
        except Exception as e:
            log.error(f"❌ Webhook 连接测试异常: {e}")
            return False


# ============ Webhook Key 验证 ============
def validate_webhook_key(key_input):
    """
    验证并提取 Webhook Key。支持:
      - 纯 UUID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
      - 完整 URL: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    """
    if not key_input:
        return None

    # 如果是完整 URL，提取 key 参数
    url_match = re.search(r'[?&]key=([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
                          key_input, re.IGNORECASE)
    if url_match:
        extracted = url_match.group(1)
        log.info(f"已从 URL 中提取 Webhook Key: {mask_key(extracted)}")
        return extracted

    # UUID 格式验证
    uuid_pattern = re.compile(
        r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', re.IGNORECASE
    )
    if uuid_pattern.match(key_input):
        return key_input

    log.error(f"无效的 Webhook Key 格式: {mask_key(key_input)}")
    log.error("正确格式: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
    log.error("或完整 URL: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY")
    return None


def mask_key(key):
    """脱敏 Webhook Key 用于日志输出"""
    if not key or len(key) < 12:
        return "***"
    return key[:8] + "****" + key[-4:]


# ============ 主流程 ============
def run_check(stations, min_days, client, notifier):
    """执行一轮检查（所有站点）"""
    log.info(f"{'=' * 55}")
    log.info(f"开始第 {run_check.round_num} 轮检查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"监控 {len(stations)} 个站点 | 连续天数阈值: ≥{min_days} 天")
    run_check.round_num += 1

    total_found = 0
    failed_stations = 0

    for idx, (sid, sname) in enumerate(stations):
        log.info(f"[{idx + 1}/{len(stations)}] 查询: {sname} (ID:{sid})")

        day30 = fetch_day30(client, sid)
        if day30 is None:
            log.warning(f"  ⚠ 查询失败，跳过此站点")
            failed_stations += 1
            client.smart_delay(2.0, 4.0)
            continue

        # 将值转为 int
        availability = {}
        for date_str, count in day30.items():
            try:
                availability[date_str] = int(count)
            except (ValueError, TypeError):
                availability[date_str] = 0

        total_days = len(availability)
        available_days = sum(1 for v in availability.values() if v > 0)
        log.info(f"  📊 30天中有 {available_days}/{total_days} 天可预订")

        consecutive = find_consecutive_available(availability, min_days)

        if consecutive:
            total_found += len(consecutive)
            for start, end, days in consecutive:
                log.info(f"  🎉 {start} ~ {end} ({days}天连续可预订)")
            if notifier:
                notifier.send(sid, sname, consecutive)
            else:
                log.info("  (未配置Webhook，仅控制台输出)")
        else:
            log.info(f"  ❌ 无连续 ≥{min_days} 天区间")

        # 站点间反检测延迟
        if idx < len(stations) - 1:
            client.smart_delay(1.5, 3.0)

    log.info(f"{'=' * 55}")
    log.info(f"本轮摘要: 检查 {len(stations)} 个站点，"
             f"发现 {total_found} 个连续区间，{failed_stations} 个站点查询失败")


run_check.round_num = 1


# ============ 参数解析 ============
def parse_args():
    parser = argparse.ArgumentParser(
        description="深圳青年驿站自动化监控脚本 v2.0 - 监控连续可预订房间并通过企业微信通知",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  %(prog)s -x 30 -y 7 -s all -w YOUR_WEBHOOK_KEY
      每30分钟监控全部活跃站点，查找连续7天可预订区间

  %(prog)s -x 15 -y 5 -s 75,85,86 -w YOUR_KEY
      每15分钟监控指定3个站点，查找连续5天可预订区间

  %(prog)s -x 10 -y 3 -s 85 --once
      检查一次CC公寓站就退出（仅控制台输出）

  %(prog)s --list-stations
      列出所有驿站ID、名称和状态

Webhook Key 配置说明:
  1. 打开企业微信群 -> 右上角 ... -> 群机器人 -> 添加 -> 新建机器人
  2. 复制 Webhook 地址，格式如:
     https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  3. 使用 key= 后面的 UUID 作为 -w 参数值
  4. 也可直接粘贴完整 URL，脚本会自动提取 Key
        """
    )
    parser.add_argument("-x", "--interval", type=int,
                        help=f"查询间隔（分钟），最小 {MIN_INTERVAL_MINUTES}")
    parser.add_argument("-y", "--days", type=int,
                        help="连续可预订天数阈值")
    parser.add_argument("-s", "--station", type=str,
                        help="驿站: all(全部活跃站点) | 75,85,86(多站点) | 85(单站点)")
    parser.add_argument("-w", "--webhook-key", type=str, default=None,
                        help="企业微信Webhook Key或完整URL (也可通过环境变量 SZYOUTH_WEBHOOK_KEY 设置)")
    parser.add_argument("--list-stations", action="store_true",
                        help="列出所有可用驿站")
    parser.add_argument("--once", action="store_true",
                        help="只执行一次检查后退出，不循环")

    args = parser.parse_args()

    # 列出站点
    if args.list_stations:
        print("\n正在获取驿站列表...")
        client = APIClient()
        list_all_stations(client)
        sys.exit(0)

    # 验证必需参数
    errors = []
    if args.interval is None:
        errors.append("  -x/--interval: 查询间隔（分钟）")
    elif args.interval < MIN_INTERVAL_MINUTES:
        errors.append(f"  -x/--interval: 最小 {MIN_INTERVAL_MINUTES} 分钟（防止请求过于频繁）")
    if args.days is None:
        errors.append("  -y/--days: 连续天数阈值")
    elif args.days < 1:
        errors.append("  -y/--days: 必须 >= 1")
    if args.station is None:
        errors.append("  -s/--station: 驿站ID (all / 75,85,86 / 85)")

    if errors:
        print("❌ 缺少必需参数:\n" + "\n".join(errors))
        print("\n示例:")
        print("  python szyouth_monitor.py -x 30 -y 7 -s all -w YOUR_WEBHOOK_KEY")
        print("  python szyouth_monitor.py -x 15 -y 5 -s 75,85,86 -w YOUR_KEY")
        print("  python szyouth_monitor.py --list-stations")
        print("\n使用 -h 查看完整帮助")
        sys.exit(1)

    # Webhook key: 命令行参数 > 环境变量
    if not args.webhook_key:
        args.webhook_key = os.environ.get("SZYOUTH_WEBHOOK_KEY")

    if args.webhook_key:
        args.webhook_key = validate_webhook_key(args.webhook_key)
        if args.webhook_key is None:
            sys.exit(1)
    else:
        log.warning("未配置企业微信Webhook Key，将仅在控制台输出结果")
        log.warning("可通过 -w 参数或 SZYOUTH_WEBHOOK_KEY 环境变量设置")

    return args


def list_all_stations(client):
    """获取并展示所有可用驿站（含分页）"""
    stations = fetch_all_stations(client)
    if not stations:
        print("获取失败，请检查网络连接")
        return

    active = [s for s in stations if s.get("status") == 1000]
    inactive = [s for s in stations if s.get("status") != 1000]

    print(f"\n{'ID':<6}{'状态':<6}{'名称'}")
    print("-" * 60)
    for s in active:
        print(f"{s['id']:<6}{'✅':<6}{s['name']}")
    for s in inactive:
        print(f"{s['id']:<6}{'❌':<6}{s['name']}")

    print(f"\n共 {len(stations)} 个站点，其中 {len(active)} 个活跃，{len(inactive)} 个已停用")
    print("使用 -s all 可监控全部活跃站点")


# ============ 入口 ============
def main():
    args = parse_args()

    client = APIClient()

    # 解析站点列表
    stations = resolve_station_ids(args.station, client)
    if not stations:
        log.error("未找到有效站点，退出")
        sys.exit(1)

    log.info("=" * 60)
    log.info("深圳青年驿站自动化监控 v2.0 启动")
    log.info(f"监控站点: {len(stations)} 个")
    for sid, sname in stations:
        log.info(f"  - {sname} (ID:{sid})")
    log.info(f"参数: 间隔={args.interval}分钟 连续天数阈值=≥{args.days}天")
    log.info(f"Webhook: {'已配置' if args.webhook_key else '未配置(仅控制台输出)'}")
    log.info("=" * 60)

    notifier = None
    if args.webhook_key:
        notifier = NotificationManager(args.webhook_key)
        if not notifier.test_webhook():
            log.error("❌ Webhook 测试失败，请检查 Key 后重试")
            sys.exit(1)

    # 优雅退出
    running = True

    def signal_handler(sig, frame):
        nonlocal running
        log.info("\n收到退出信号，正在安全退出...")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if args.once:
        run_check(stations, args.days, client, notifier)
        return

    while running:
        try:
            run_check(stations, args.days, client, notifier)
        except Exception as e:
            log.exception(f"本轮检查出现未预期异常: {e}")

        if not running:
            break

        next_time = (datetime.now() + timedelta(minutes=args.interval)).strftime('%H:%M:%S')
        log.info(f"下次检查: {next_time} (间隔 {args.interval} 分钟)")
        # 使用小间隔sleep以支持及时退出
        for _ in range(args.interval * 60):
            if not running:
                break
            time.sleep(1)

    log.info("监控已安全退出")


if __name__ == "__main__":
    main()

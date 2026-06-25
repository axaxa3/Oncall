"""智能运维监控 MCP Server

本地实现的监控服务 MCP Server，提供：
- 监控数据查询（CPU、内存、磁盘、网络等）
- 进程信息查询

数据来源：
- Prometheus 查询（适用于 Linux 环境，通过 Node Exporter）
- 本地 Windows 磁盘查询（通过 psutil，适用于 Windows 本机）
- Prometheus 不可用时 fallback 到模拟数据
"""

import logging
import functools
import json
import random
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from fastmcp import FastMCP

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Monitor_MCP_Server")

mcp = FastMCP("Monitor")


def log_tool_call(func):
    """装饰器：记录工具调用的日志，包括方法名、参数和返回状态"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        method_name = func.__name__

        logger.info(f"=" * 80)
        logger.info(f"调用方法: {method_name}")

        if kwargs:
            try:
                params_str = json.dumps(kwargs, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                params_str = str(kwargs)
            logger.info(f"参数信息:\n{params_str}")
        else:
            logger.info("参数信息: 无")

        try:
            result = func(*args, **kwargs)
            logger.info(f"返回状态: SUCCESS")

            if isinstance(result, dict):
                summary = {k: v if not isinstance(v, (list, dict)) else f"<{type(v).__name__} with {len(v)} items>"
                          for k, v in list(result.items())[:5]}
                logger.info(f"返回结果摘要: {json.dumps(summary, ensure_ascii=False)}")
            else:
                logger.info(f"返回结果: {result}")

            logger.info(f"=" * 80)
            return result

        except Exception as e:
            logger.error(f"返回状态: ERROR")
            logger.error(f"错误信息: {str(e)}")
            logger.error(f"=" * 80)
            raise

    return wrapper


# ============================================================
# Prometheus 配置与客户端
# ============================================================

PROMETHEUS_BASE_URL: str = "http://127.0.0.1:9090"
PROMETHEUS_REQUEST_TIMEOUT: float = 10.0

try:
    from dotenv import load_dotenv
    load_dotenv()
    import os
    PROMETHEUS_BASE_URL = os.getenv("PROMETHEUS_BASE_URL", PROMETHEUS_BASE_URL)
    PROMETHEUS_REQUEST_TIMEOUT = float(os.getenv("PROMETHEUS_REQUEST_TIMEOUT", PROMETHEUS_REQUEST_TIMEOUT))
except ImportError:
    pass


def query_prometheus(promql: str, start: Optional[str] = None, end: Optional[str] = None,
                     step: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """查询 Prometheus /api/v1/query_range。

    Args:
        promql: PromQL 查询表达式
        start: 开始时间（Unix 时间戳秒数，字符串）
        end: 结束时间（Unix 时间戳秒数，字符串）
        step: 查询步长（如 "60s", "5m"）

    Returns:
        Prometheus data 字典（含 resultType 和 result），失败时返回 None
    """
    import httpx

    base_url = PROMETHEUS_BASE_URL.rstrip("/")
    api_url = f"{base_url}/api/v1/query_range"

    params = {"query": promql}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    if step:
        params["step"] = step

    try:
        with httpx.Client(timeout=PROMETHEUS_REQUEST_TIMEOUT) as client:
            resp = client.get(api_url, params=params)
            resp.raise_for_status()
            body = resp.json()
            if body.get("status") == "success":
                return body.get("data", {})
            logger.warning(f"Prometheus query failed: {body}")
            return None
    except httpx.HTTPError as e:
        logger.warning(f"Prometheus connection failed ({api_url}): {e}")
        return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Prometheus response parse failed: {e}")
        return None


def query_prometheus_single(promql: str, time: Optional[str] = None) -> Optional[float]:
    """查询 Prometheus /api/v1/query 瞬时值。

    Returns:
        指标值（float），失败时返回 None
    """
    import httpx

    base_url = PROMETHEUS_BASE_URL.rstrip("/")
    api_url = f"{base_url}/api/v1/query"

    params = {"query": promql}
    if time:
        params["time"] = time

    try:
        with httpx.Client(timeout=PROMETHEUS_REQUEST_TIMEOUT) as client:
            resp = client.get(api_url, params=params)
            resp.raise_for_status()
            body = resp.json()
            if body.get("status") == "success":
                result = body.get("data", {}).get("result", [])
                if result:
                    value = result[0].get("value", [None, "0"])
                    return float(value[1])
            return None
    except httpx.HTTPError as e:
        logger.warning(f"Prometheus connection failed ({api_url}): {e}")
        return None
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"Prometheus response parse failed: {e}")
        return None


def epoch_timestamp(dt: datetime) -> str:
    """将 datetime 转为 Unix 时间戳秒数（字符串），供 Prometheus API 使用。"""
    return str(int(dt.timestamp()))


def parse_step(interval: str) -> str:
    """将 interval 字符串转为 Prometheus step 格式。"""
    if interval.endswith('m'):
        val = int(interval[:-1])
        return f"{max(val * 60, 60)}s"
    elif interval.endswith('h'):
        val = int(interval[:-1]) * 60
        return f"{val}m"
    return "60s"


def parse_time_or_default(time_str: Optional[str], default_offset_hours: int = 0) -> datetime:
    """解析时间字符串或返回默认时间。"""
    if time_str:
        try:
            return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return datetime.now() + timedelta(hours=default_offset_hours)


def build_data_points(raw_values: List[List[str]], base_time: datetime,
                      interval_minutes: int, extra: Optional[Dict] = None) -> List[Dict]:
    """将 Prometheus 原始值列表转为统一的数据点格式。

    raw_values 格式: [["timestamp_str", "value_str"], ...]
    """
    data_points = []
    for i, (ts_str, val_str) in enumerate(raw_values):
        try:
            ts = float(ts_str)
            value = float(val_str)
        except (ValueError, TypeError):
            continue
        dt = datetime.fromtimestamp(ts)
        point = {"timestamp": dt.strftime("%H:%M"), "value": round(value, 2)}
        if extra:
            point.update(extra)
        data_points.append(point)
    return data_points


def compute_stats(values: List[float], thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """计算统计数据。"""
    if not values:
        return {}
    avg_v = round(sum(values) / len(values), 2)
    max_v = round(max(values), 2)
    min_v = round(min(values), 2)
    sv = sorted(values)
    p95_v = round(sv[max(0, int(len(sv) * 0.95) - 1)], 2)

    stats = {"avg": avg_v, "max": max_v, "min": min_v, "p95": p95_v}
    if thresholds:
        for key, threshold in thresholds.items():
            stats[f"{key}_detected"] = max_v > threshold
    return stats


# ============================================================
# 模拟数据 Fallback（Prometheus 不可用时使用）
# ============================================================

def gen_fallback_cpu(start_dt: datetime, end_dt: datetime, interval_minutes: int) -> List[Dict]:
    data_points = []
    ct = start_dt
    idx = 0
    base = 10.0
    while ct <= end_dt:
        if idx < 3:
            v = base + idx * 0.5
        else:
            v = min(base + (idx - 2) * 8.5, 96.0)
        v = round(max(0, min(100, v + random.uniform(-2, 2))), 1)
        data_points.append({"timestamp": ct.strftime("%H:%M"), "value": v, "process_id": "pid-12345"})
        ct += timedelta(minutes=interval_minutes)
        idx += 1
    return data_points


def gen_fallback_memory(start_dt: datetime, end_dt: datetime, interval_minutes: int) -> List[Dict]:
    data_points = []
    ct = start_dt
    idx = 0
    base = 30.0
    total_gb = 8.0
    while ct <= end_dt:
        if idx < 3:
            v = base + idx * 1.0
        else:
            v = min(base + (idx - 2) * 5.5, 85.0)
        v = round(max(0, min(100, v + random.uniform(-1, 1))), 1)
        data_points.append({
            "timestamp": ct.strftime("%H:%M"), "value": v,
            "used_gb": round((v / 100.0) * total_gb, 2), "total_gb": total_gb
        })
        ct += timedelta(minutes=interval_minutes)
        idx += 1
    return data_points


def gen_fallback_disk(start_dt: datetime, end_dt: datetime, interval_minutes: int) -> List[Dict]:
    data_points = []
    ct = start_dt
    idx = 0
    base = 45.0
    while ct <= end_dt:
        if idx < 3:
            v = base + idx * 0.3
        else:
            v = min(base + (idx - 2) * 3.0, 88.0)
        v = round(max(0, min(100, v + random.uniform(-1, 1))), 1)
        data_points.append({"timestamp": ct.strftime("%H:%M"), "value": v, "mount_point": "/"})
        ct += timedelta(minutes=interval_minutes)
        idx += 1
    return data_points


def gen_fallback_network(start_dt: datetime, end_dt: datetime, interval_minutes: int) -> List[Dict]:
    data_points = []
    ct = start_dt
    idx = 0
    while ct <= end_dt:
        rx = round(random.uniform(10, 100) + idx * 5, 2)
        tx = round(random.uniform(5, 50) + idx * 3, 2)
        data_points.append({
            "timestamp": ct.strftime("%H:%M"),
            "rx_bytes_per_sec": round(rx * 1_000_000 / 8, 2),
            "tx_bytes_per_sec": round(tx * 1_000_000 / 8, 2),
            "rx_mbps": rx, "tx_mbps": tx
        })
        ct += timedelta(minutes=interval_minutes)
        idx += 1
    return data_points


# ============================================================
# 监控数据查询工具
# ============================================================

@mcp.tool()
@log_tool_call
def query_cpu_metrics(
    service_name: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "1m"
) -> Dict[str, Any]:
    """查询服务的 CPU 使用率监控数据。

    数据来源：优先从 Prometheus 查询 node_cpu_seconds_total，
    如果 Prometheus 不可用则 fallback 到模拟数据。

    Args:
        service_name: 服务名称（必填）
        start_time: 开始时间（可选），格式: "YYYY-MM-DD HH:MM:SS"
        end_time: 结束时间（可选），格式: "YYYY-MM-DD HH:MM:SS"
        interval: 数据聚合间隔（可选），可选值: "1m", "5m", "1h"

    Returns:
        Dict: CPU 监控数据，包含 data_source 字段标明数据来源
    """
    start_dt = parse_time_or_default(start_time, default_offset_hours=-1)
    end_dt = parse_time_or_default(end_time, default_offset_hours=0)

    interval_minutes = 1
    if interval.endswith('m'):
        interval_minutes = int(interval[:-1])
    elif interval.endswith('h'):
        interval_minutes = int(interval[:-1]) * 60

    step = parse_step(interval)
    start_ep = epoch_timestamp(start_dt)
    end_ep = epoch_timestamp(end_dt)

    # PromQL: 计算 CPU 使用率 = 100 * (1 - idle_rate)
    promql = (
        f'100 * (1 - avg(rate(node_cpu_seconds_total{{mode="idle", '
        f'job="node_exporter"}}[{step}])) by (instance))'
    )

    result = query_prometheus(promql, start=start_ep, end=end_ep, step=step)

    if result and result.get("result"):
        raw_values = result["result"][0].get("values", [])
        data_points = build_data_points(raw_values, start_dt, interval_minutes)
        if data_points:
            source = "Prometheus"
        else:
            data_points = gen_fallback_cpu(start_dt, end_dt, interval_minutes)
            source = "模拟数据 (Prometheus 无数据)"
    else:
        data_points = gen_fallback_cpu(start_dt, end_dt, interval_minutes)
        source = "模拟数据 (Prometheus 不可用)"

    if data_points:
        values = [d["value"] for d in data_points]
        stats = compute_stats(values, {"spike": 80.0})
        spike = stats.pop("spike_detected", False)

        return {
            "service_name": service_name,
            "metric_name": "cpu_usage_percent",
            "interval": interval,
            "data_source": source,
            "data_points": data_points,
            "statistics": stats,
            "alert_info": {
                "triggered": spike,
                "threshold": 80.0,
                "message": "CPU 使用率持续超过 80% 阈值" if spike else "CPU 使用率正常"
            }
        }
    return {
        "service_name": service_name, "metric_name": "cpu_usage_percent",
        "interval": interval, "data_source": source,
        "data_points": [], "statistics": {}, "error": "未能获取 CPU 数据"
    }


@mcp.tool()
@log_tool_call
def query_memory_metrics(
    service_name: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "1m"
) -> Dict[str, Any]:
    """查询服务的内存使用监控数据。

    数据来源：优先从 Prometheus 查询 node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes。
    """
    start_dt = parse_time_or_default(start_time, default_offset_hours=-1)
    end_dt = parse_time_or_default(end_time, default_offset_hours=0)

    interval_minutes = 1
    if interval.endswith('m'):
        interval_minutes = int(interval[:-1])
    elif interval.endswith('h'):
        interval_minutes = int(interval[:-1]) * 60

    step = parse_step(interval)
    start_ep = epoch_timestamp(start_dt)
    end_ep = epoch_timestamp(end_dt)

    # 内存使用率 = (1 - MemAvailable / MemTotal) * 100
    promql = (
        f'100 * (1 - avg(node_memory_MemAvailable_bytes{{job="node_exporter"}} '
        f'/ node_memory_MemTotal_bytes{{job="node_exporter"}}) by (instance))'
    )

    result = query_prometheus(promql, start=start_ep, end=end_ep, step=step)

    if result and result.get("result"):
        raw_values = result["result"][0].get("values", [])
        data_points = build_data_points(raw_values, start_dt, interval_minutes)
        if data_points:
            source = "Prometheus"
        else:
            data_points = gen_fallback_memory(start_dt, end_dt, interval_minutes)
            source = "模拟数据 (Prometheus 无数据)"
    else:
        data_points = gen_fallback_memory(start_dt, end_dt, interval_minutes)
        source = "模拟数据 (Prometheus 不可用)"

    if data_points:
        values = [d["value"] for d in data_points]
        avg_v = round(sum(values) / len(values), 2)
        max_v = round(max(values), 2)
        min_v = round(min(values), 2)
        sv = sorted(values)
        p95_v = round(sv[max(0, int(len(sv) * 0.95) - 1)], 2)
        pressure = max_v > 70.0

        return {
            "service_name": service_name,
            "metric_name": "memory_usage_percent",
            "interval": interval,
            "data_source": source,
            "data_points": data_points,
            "statistics": {
                "avg": avg_v, "max": max_v, "min": min_v, "p95": p95_v,
                "memory_pressure": pressure
            },
            "alert_info": {
                "triggered": pressure,
                "threshold": 70.0,
                "message": "内存使用率超过 70% 阈值，存在内存压力" if pressure else "内存使用率正常"
            }
        }
    return {
        "service_name": service_name, "metric_name": "memory_usage_percent",
        "interval": interval, "data_source": source,
        "data_points": [], "statistics": {}, "error": "未能获取内存数据"
    }


@mcp.tool()
@log_tool_call
def query_disk_metrics(
    service_name: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "1m"
) -> Dict[str, Any]:
    """查询服务的磁盘使用监控数据。

    数据来源：优先从 Prometheus 查询 node_filesystem_avail_bytes / node_filesystem_size_bytes。
    """
    start_dt = parse_time_or_default(start_time, default_offset_hours=-1)
    end_dt = parse_time_or_default(end_time, default_offset_hours=0)

    interval_minutes = 1
    if interval.endswith('m'):
        interval_minutes = int(interval[:-1])
    elif interval.endswith('h'):
        interval_minutes = int(interval[:-1]) * 60

    step = parse_step(interval)
    start_ep = epoch_timestamp(start_dt)
    end_ep = epoch_timestamp(end_dt)

    # 磁盘使用率 = (1 - avail / size) * 100
    # 注意：不要用 max by (instance) 包裹后再除法，PromQL 不支持这种跨指标的去重除法。
    # 直接用 avail / size 让 PromQL 按全量标签自动对齐，再用 Python 去重。
    promql = (
        f'100 * (1 - node_filesystem_avail_bytes{{fstype="ext4", '
        f'job="node_exporter"}} / node_filesystem_size_bytes{{fstype="ext4", '
        f'job="node_exporter"}})'
    )

    result = query_prometheus(promql, start=start_ep, end=end_ep, step=step)

    if result and result.get("result"):
        series_list = result["result"]
        # 去重：同一 device+fstype 可能有多个重复 mountpoint（Docker Desktop WSL 特性），
        # 只保留最短的那个（通常是真实挂载点）。
        seen_devices: Dict[str, str] = {}
        for series in series_list:
            labels = series.get("labels", {})
            dev = labels.get("device", "unknown")
            mp = labels.get("mountpoint", "")
            if dev not in seen_devices or len(mp) < len(seen_devices[dev]):
                seen_devices[dev] = mp
        dedup_set = set(seen_devices.values())
        # 只保留去重后的 series
        deduped_series = [s for s in series_list if s.get("labels", {}).get("mountpoint", "") in dedup_set]
        if deduped_series:
            # 取第一个 series 的 values（所有 series 的 values 相同，只是标签不同）
            raw_values = deduped_series[0].get("values", [])
            data_points = build_data_points(raw_values, start_dt, interval_minutes)
            source = "Prometheus"
        else:
            data_points = gen_fallback_disk(start_dt, end_dt, interval_minutes)
            source = "模拟数据 (Prometheus 无数据)"
    else:
        data_points = gen_fallback_disk(start_dt, end_dt, interval_minutes)
        source = "模拟数据 (Prometheus 不可用)"

    if data_points:
        values = [d["value"] for d in data_points]
        avg_v = round(sum(values) / len(values), 2)
        max_v = round(max(values), 2)
        min_v = round(min(values), 2)
        sv = sorted(values)
        p95_v = round(sv[max(0, int(len(sv) * 0.95) - 1)], 2)
        warning = max_v > 75.0
        critical = max_v > 90.0

        return {
            "service_name": service_name,
            "metric_name": "disk_usage_percent",
            "interval": interval,
            "data_source": source,
            "data_points": data_points,
            "statistics": {
                "avg": avg_v, "max": max_v, "min": min_v, "p95": p95_v,
                "disk_warning": warning, "disk_critical": critical
            },
            "alert_info": {
                "triggered": warning,
                "threshold": 75.0,
                "level": "critical" if critical else "warning",
                "message": "磁盘使用率严重超标（超过 90%）" if critical else (
                    "磁盘使用率超过 75% 阈值，请注意清理" if warning else "磁盘使用率正常"
                )
            }
        }
    return {
        "service_name": service_name, "metric_name": "disk_usage_percent",
        "interval": interval, "data_source": source,
        "data_points": [], "statistics": {}, "error": "未能获取磁盘数据"
    }


@mcp.tool()
@log_tool_call
def query_network_metrics(
    service_name: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "1m"
) -> Dict[str, Any]:
    """查询服务的网络流量监控数据。

    数据来源：优先从 Prometheus 查询 node_network_receive_bytes_total / node_network_transmit_bytes_total。
    """
    start_dt = parse_time_or_default(start_time, default_offset_hours=-1)
    end_dt = parse_time_or_default(end_time, default_offset_hours=0)

    interval_minutes = 1
    if interval.endswith('m'):
        interval_minutes = int(interval[:-1])
    elif interval.endswith('h'):
        interval_minutes = int(interval[:-1]) * 60

    step = parse_step(interval)
    start_ep = epoch_timestamp(start_dt)
    end_ep = epoch_timestamp(end_dt)

    # 网络收发速率（bytes/s），用 max by 去重
    promql_rx = (
        f'max by (instance) (rate(node_network_receive_bytes_total{{device!="lo", '
        f'job="node_exporter"}}[{step}]))'
    )
    promql_tx = (
        f'max by (instance) (rate(node_network_transmit_bytes_total{{device!="lo", '
        f'job="node_exporter"}}[{step}]))'
    )

    result_rx = query_prometheus(promql_rx, start=start_ep, end=end_ep, step=step)
    result_tx = query_prometheus(promql_tx, start=start_ep, end=end_ep, step=step)

    source = "Prometheus"
    rx_raw = result_rx["result"][0].get("values", []) if result_rx and result_rx.get("result") else []
    tx_raw = result_tx["result"][0].get("values", []) if result_tx and result_tx.get("result") else []

    if not rx_raw or not tx_raw:
        source = "模拟数据 (Prometheus 不可用)"
        rx_raw = [(str(int(start_dt.timestamp())), "0")] * 10
        tx_raw = [(str(int(start_dt.timestamp())), "0")] * 10

    data_points = []
    ct = start_dt
    for i, (rx_ts, rx_val) in enumerate(rx_raw):
        try:
            rx_bps = float(rx_val)
            tx_ts, tx_val = tx_raw[i] if i < len(tx_raw) else ("0", "0")
            tx_bps = float(tx_val)
        except (ValueError, IndexError):
            continue
        data_points.append({
            "timestamp": ct.strftime("%H:%M"),
            "rx_bytes_per_sec": round(rx_bps, 2),
            "tx_bytes_per_sec": round(tx_bps, 2),
            "rx_mbps": round(rx_bps * 8 / 1_000_000, 2),
            "tx_mbps": round(tx_bps * 8 / 1_000_000, 2),
        })
        ct += timedelta(minutes=interval_minutes)

    if not data_points:
        data_points = gen_fallback_network(start_dt, end_dt, interval_minutes)
        source = "模拟数据 (Prometheus 无数据)"

    if data_points:
        rx_rates = [d["rx_mbps"] for d in data_points]
        tx_rates = [d["tx_mbps"] for d in data_points]
        return {
            "service_name": service_name,
            "metric_name": "network_throughput_mbps",
            "interval": interval,
            "data_source": source,
            "data_points": data_points,
            "statistics": {
                "rx_avg_mbps": round(sum(rx_rates) / len(rx_rates), 2),
                "rx_max_mbps": round(max(rx_rates), 2),
                "tx_avg_mbps": round(sum(tx_rates) / len(tx_rates), 2),
                "tx_max_mbps": round(max(tx_rates), 2),
            },
            "alert_info": {
                "triggered": False, "threshold": 1000.0,
                "message": "网络流量正常"
            }
        }
    return {
        "service_name": service_name, "metric_name": "network_throughput_mbps",
        "interval": interval, "data_source": source,
        "data_points": [], "statistics": {}, "error": "未能获取网络数据"
    }


@mcp.tool()
@log_tool_call
def query_process_info(
    service_name: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Dict[str, Any]:
    """查询服务的进程运行状态信息。

    数据来源：优先从 Prometheus 查询 node_procs_running / node_procs_blocked。
    """
    promql_running = 'node_procs_running{job="node_exporter"}'
    promql_blocked = 'node_procs_blocked{job="node_exporter"}'

    running = query_prometheus_single(promql_running)
    blocked = query_prometheus_single(promql_blocked)

    if running is not None or blocked is not None:
        source = "Prometheus"
        process_info = {"running": running if running is not None else 0, "blocked": blocked if blocked is not None else 0}
    else:
        source = "模拟数据 (Prometheus 不可用)"
        process_info = {"running": random.randint(1, 5), "blocked": random.randint(0, 2)}

    return {
        "service_name": service_name,
        "data_source": source,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "process_info": process_info,
        "alert_info": {
            "triggered": process_info.get("blocked", 0) > 3,
            "threshold": 3,
            "message": "存在阻塞进程" if process_info.get("blocked", 0) > 3 else "进程状态正常"
        }
    }


# ============================================================
# 本地 Windows 磁盘查询（psutil）
# ============================================================

@mcp.tool()
@log_tool_call
def query_local_disk_metrics() -> Dict[str, Any]:
    """查询本机 Windows 磁盘使用率（通过 psutil，不依赖 Prometheus）。

    适用于 Windows 本机环境，直接读取本地磁盘分区信息。

    Returns:
        Dict: 所有本地磁盘分区的使用情况，包含:
            - disks: 磁盘列表，每个包含:
                * device: 设备名 (如 C:, D:)
                * label: 卷标
                * total_gb: 总容量 (GB)
                * used_gb: 已用容量 (GB)
                * free_gb: 剩余容量 (GB)
                * usage_percent: 使用率 (%)
                * mount_point: 挂载点
                * warning: 是否超过 75% 阈值
                * critical: 是否超过 90% 阈值
            - overall_alert: 最高级别的告警信息
    """
    try:
        import psutil
    except ImportError:
        return {
            "error": "psutil 未安装，请运行: pip install psutil",
            "disks": []
        }

    partitions = psutil.disk_partitions(all=False)
    disks = []
    max_usage = 0.0
    max_level = "normal"

    for p in partitions:
        try:
            usage = psutil.disk_usage(p.mountpoint)
        except (PermissionError, OSError):
            continue

        total_gb = round(usage.total / (1024**3), 2)
        used_gb = round(usage.used / (1024**3), 2)
        free_gb = round(usage.free / (1024**3), 2)
        usage_percent = round(usage.percent, 1)

        warning = usage_percent > 75.0
        critical = usage_percent > 90.0

        disks.append({
            "device": p.device,
            "label": getattr(p, "label", "") or "",
            "mount_point": p.mountpoint,
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "usage_percent": usage_percent,
            "warning": warning,
            "critical": critical,
        })

        if usage_percent > max_usage:
            max_usage = usage_percent
            if critical:
                max_level = "critical"
            elif warning:
                max_level = "warning"

    # 按使用率排序，高的在前
    disks.sort(key=lambda x: x["usage_percent"], reverse=True)

    alert_message = "所有磁盘使用率正常"
    if max_level == "critical":
        alert_message = f"磁盘使用率严重超标！最高使用率: {max_usage}%"
    elif max_level == "warning":
        alert_message = f"磁盘使用率较高，请注意清理。最高使用率: {max_usage}%"

    return {
        "data_source": "本地 psutil",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "disks": disks,
        "overall_alert": {
            "level": max_level,
            "max_usage_percent": round(max_usage, 1),
            "message": alert_message
        }
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8004, path="/mcp")

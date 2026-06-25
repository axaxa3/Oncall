# 监控指标查询体系文档

## 一、整体架构

```
用户提问 ("查一下磁盘")
    │
    ▼
RAG Agent (rag_agent_service.py)
    │
    ▼
MCP Client (mcp_client.py) ── MultiServerMCPClient ──┐
    │                                                 │
    ▼                                               ▼
本地工具 (DEFAULT_LOCAL_AGENT_TOOLS)        MCP 服务器
  - query_prometheus_alerts                  - monitor_server.py (port 8004)
  - web_search_mcp                           - cls_server.py (port 8003)
                                            │
                                            ▼
                                     monitor_server.py 的 @mcp.tool() 方法
```

## 二、monitor_server.py 中的 6 个工具

### 1. 通过 Prometheus 查询的工具（5 个）

这 5 个工具共享同一条数据链路：

```
工具函数 (query_xxx_metrics)
    │
    ├─ ① 解析参数 → 确定时间范围和步长
    │   parse_time_or_default(start_time, hours=-1)  → 默认查过去 1 小时
    │   parse_step(interval) → "1m" → "60s"
    │
    ├─ ② 构造 PromQL 查询
    │   拼接 node_exporter 的指标名 + 标签过滤
    │
    ├─ ③ 调用 Prometheus API
    │   query_prometheus() → GET /api/v1/query_range  (时序数据)
    │   query_prometheus_single() → GET /api/v1/query  (瞬时值)
    │
    ├─ ④ 判断结果
    │   ├─ 有数据 → build_data_points() 格式化 → 计算统计 → 返回
    │   ├─ 无数据 (空 result) → gen_fallback_xxx() 模拟数据
    │   └─ 连接失败 → gen_fallback_xxx() 模拟数据
    │
    └─ ⑤ 返回结构化结果 (data_source 字段标明来源)
```

| 工具 | PromQL 指标 | 查询方式 | 用途 |
|------|------------|---------|------|
| **query_cpu_metrics** | `node_cpu_seconds_total{mode="idle"}` | query_range (时序) | CPU 使用率趋势 |
| **query_memory_metrics** | `node_memory_MemAvailable_bytes / MemTotal_bytes` | query_range (时序) | 内存使用率趋势 |
| **query_disk_metrics** | `node_filesystem_avail_bytes / size_bytes` | query_range (时序) | 磁盘使用率趋势 |
| **query_network_metrics** | `node_network_receive/transmit_bytes_total` | query_range (时序) | 网络流量趋势 |
| **query_process_info** | `node_procs_running / blocked` | query (瞬时) | 当前进程状态 |

### 2. 不依赖 Prometheus 的工具（1 个）

| 工具 | 数据源 | 用途 |
|------|--------|------|
| **query_local_disk_metrics** | `psutil.disk_partitions()` | Windows 本机磁盘真实数据 |

## 三、Prometheus + Node Exporter 的运行逻辑

```
┌─────────────────────────────────────────────────────────────┐
│  Docker Compose (docker-compose.monitor.yml)               │
│                                                             │
│  ┌──────────────────┐    ┌──────────────────────────┐      │
│  │   Prometheus      │    │   Node Exporter           │      │
│  │   (port 9090)     │    │   (port 9100)             │      │
│  │                   │    │                           │      │
│  │  scrape_configs:  │◄───│  暴露 /metrics HTTP 接口   │      │
│  │    - job:         │    │  GET /metrics             │      │
│  │      node_exporter│    │                           │      │
│  │      target:      │    │  采集系统指标:              │      │
│  │      host.docker │    │    • CPU 时间 (seconds)     │      │
│  │      .internal:  │    │    • 内存 (bytes)           │      │
│  │      9100         │    │    • 磁盘 (bytes/count)     │      │
│  │                   │    │    • 网络 (bytes/packets)   │      │
│  │  每15秒抓取一次    │    │    • 进程数                 │      │
│  │  存储为时序数据    │    │                           │      │
│  │  保留15天         │    │  通过 volume 挂载:          │      │
│  └──────────────────┘    │    /proc → /host/proc      │      │
│                          │    /sys  → /host/sys       │      │
│                          │    /    → /rootfs           │      │
│                          └──────────────────────────┘      │
│                                                             │
│  关键: Node Exporter 在 WSL 2 虚拟机里运行                    │
│  看到的是 Linux 虚拟磁盘, 不是 Windows 物理盘                  │
└─────────────────────────────────────────────────────────────┘
```

### 数据流向

```
硬件/OS (Linux kernel)
    ↓ (kernel 暴露指标)
/proc /sys /rootfs (通过 volume 挂载到容器)
    ↓ (Node Exporter 读取)
Node Exporter (port 9100)
    ↓ (HTTP /metrics 暴露)
Prometheus (port 9090)
    ↓ (每15秒 scrape)
时序数据库 (TSDB)
    ↓ (用户/程序查询)
/api/v1/query 或 /api/v1/query_range
    ↓ (httpx 客户端)
monitor_server.py 的工具函数
    ↓ (格式化)
返回给 Agent → 返回给用户
```

### Prometheus 配置 (`prometheus.yml`)

```yaml
scrape_configs:
  # 1. Prometheus 自监控
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]

  # 2. Node Exporter — 系统指标
  - job_name: "node_exporter"
    static_configs:
      - targets: ["host.docker.internal:9100"]  # Docker 宿主机访问
```

## 四、三种数据来源的优先级

每个工具函数的逻辑都是 **三级降级**：

```
第一优先: Prometheus 真实数据
  │
  ├─ 成功且有数据 → 返回真实数据 ✓
  │
  ├─ 成功但无数据 → 降级
  │     (如: ext4 文件系统查询返回空, 因为 WSL 的 mountpoint 太多)
  │
  ▼
第二优先: 模拟数据 (fallback)
  │
  ├─ gen_fallback_cpu/memory/disk/network()
  │   生成随机但有规律的假数据
  │   标记: data_source = "模拟数据 (Prometheus 无数据)"
  │
  ▼
第三优先: 连接失败时的模拟数据
  │
  ├─ Prometheus 连不上 → 同样走模拟数据
  │   标记: data_source = "模拟数据 (Prometheus 不可用)"
```

**`query_local_disk_metrics` 是例外** — 它不走 Prometheus，直接用 `psutil` 读取 Windows 本机磁盘，所以拿到的才是真实的 C: 盘和 D: 盘数据。

## 五、各模块工作原理详解

### 5.1 query_cpu_metrics — CPU 使用率查询

**工作原理：**

1. 将用户传入的时间范围（默认最近 1 小时）转为 Unix 时间戳
2. 构造 PromQL：`100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle", job="node_exporter"}[5m])) by (instance))`
   - `rate()` 计算每秒 CPU 空闲时间的增长率
   - `mode="idle"` 只统计空闲时间
   - `1 - idle_rate` 就是使用率
   - `avg(...) by (instance)` 按实例取平均（多核 CPU 会自动聚合）
3. 调用 Prometheus `/api/v1/query_range` 获取时序数据
4. 将返回的 `[[timestamp, value], ...]` 格式转为 `{"timestamp": "HH:MM", "value": xx.xx}` 格式
5. 计算 avg/max/min/p95 统计值，判断是否有 CPU 尖峰（>80%）

**注意：** 在 Windows 环境下，Node Exporter 跑在 WSL 2 虚拟机中，采集的是 Linux 虚拟机的 CPU 数据，不是 Windows 本机 CPU。

### 5.2 query_memory_metrics — 内存使用率查询

**工作原理：**

1. PromQL：`100 * (1 - avg(node_memory_MemAvailable_bytes{job="node_exporter"} / node_memory_MemTotal_bytes{job="node_exporter"}) by (instance))`
2. 用 `MemAvailable / MemTotal` 计算空闲比例，`1 - 空闲比例` 就是使用率
3. 其余流程同 CPU 查询
4. 判断内存压力：使用率 >70% 标记为存在压力

### 5.3 query_disk_metrics — 磁盘使用率查询

**工作原理：**

1. PromQL：`100 * (1 - node_filesystem_avail_bytes{fstype="ext4", job="node_exporter"} / node_filesystem_size_bytes{fstype="ext4", job="node_exporter"})`
   - 直接让 PromQL 按全量标签自动对齐计算 avail/size
   - **不能用** `max by (instance)` 分别包裹两个指标后再除法（PromQL 不支持这种跨指标的去重除法）
2. 返回结果中，同一物理磁盘可能有多个重复 mountpoint（Docker Desktop WSL 特性），如：
   - `/mnt/docker-desktop-disk`
   - `/parent-distro/mnt/docker-desktop-disk`
   - `/run/desktop/mnt/docker-desktop-disk`
3. 去重逻辑：按 `device` 分组，每组保留 mountpoint 最短的一条
4. 告警阈值：>75% warning，>90% critical

### 5.4 query_network_metrics — 网络流量查询

**工作原理：**

1. 分别查询接收和发送两条 PromQL：
   - RX: `max by (instance) (rate(node_network_receive_bytes_total{device!="lo", job="node_exporter"}[5m]))`
   - TX: `max by (instance) (rate(node_network_transmit_bytes_total{device!="lo", job="node_exporter"}[5m]))`
   - `device!="lo"` 排除回环接口
2. 将两个查询结果按时间戳对齐，合并为 `{"rx_mbps": xx, "tx_mbps": xx}` 格式
3. 计算收发平均/最大速率

### 5.5 query_process_info — 进程状态查询

**工作原理：**

1. 使用 `query_prometheus_single()` 调用 `/api/v1/query` 获取瞬时值
2. 查询 `node_procs_running`（运行中进程数）和 `node_procs_blocked`（阻塞进程数）
3. 任一有值即判定 Prometheus 可用，否则走模拟数据

### 5.6 query_local_disk_metrics — 本地磁盘查询

**工作原理：**

1. 不经过 Prometheus，直接用 Python `psutil` 库读取本机磁盘
2. `psutil.disk_partitions(all=False)` 获取所有物理分区（排除网络盘/光驱）
3. 对每个分区调用 `psutil.disk_usage(mountpoint)` 获取使用量
4. 按使用率从高到低排序返回
5. 告警阈值：>75% warning，>90% critical

**适用场景：** Windows 本机磁盘查询，返回真实数据（C:、D: 盘等）

## 六、已知问题

| 问题 | 影响 | 解决建议 |
|------|------|---------|
| CPU/内存/网络工具的 PromQL 只取 `result[0]` | 多实例时只看到一台机器 | 应遍历所有 series |
| 磁盘去重逻辑按 device 分组 | 能工作但逻辑粗糙 | 可以用 `topk` 或更精细的标签选择 |
| Node Exporter 在 WSL 里 | 只能看到 Linux 虚拟盘 | 用 `query_local_disk_metrics` 读 Windows 真实盘 |
| CPU/内存/网络工具在 Windows 上 | Prometheus 无对应数据，永远走模拟数据 | 要么装 Windows Exporter，要么也加 psutil 版本 |

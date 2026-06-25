#!/bin/bash
# ============================================================
# 监控栈启动脚本 — Prometheus + Node Exporter
# ============================================================
# 用法:
#   ./start_monitor.sh          # 启动所有服务
#   ./start_monitor.sh stop     # 停止所有服务
#   ./start_monitor.sh restart  # 重启所有服务
#   ./start_monitor.sh status   # 查看状态
#   ./start_monitor.sh logs     # 查看日志
# ============================================================

COMPOSE_FILE="docker-compose.monitor.yml"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "${1:-start}" in
  start)
    echo "=========================================="
    echo "  启动监控栈..."
    echo "=========================================="
    cd "$PROJECT_DIR"
    docker compose -f "$COMPOSE_FILE" up -d
    echo ""
    echo "等待服务就绪..."
    sleep 5
    echo ""
    echo "Prometheus UI:  http://localhost:9090"
    echo "Node Exporter:  http://localhost:9100/metrics"
    echo ""
    echo "运行 'docker compose -f $COMPOSE_FILE ps' 查看状态"
    ;;

  stop)
    echo "=========================================="
    echo "  停止监控栈..."
    echo "=========================================="
    cd "$PROJECT_DIR"
    docker compose -f "$COMPOSE_FILE" down
    echo "已停止。"
    ;;

  restart)
    echo "=========================================="
    echo "  重启监控栈..."
    echo "=========================================="
    cd "$PROJECT_DIR"
    docker compose -f "$COMPOSE_FILE" restart
    sleep 3
    echo "已重启。"
    docker compose -f "$COMPOSE_FILE" ps
    ;;

  status)
    echo "=========================================="
    echo "  监控栈状态"
    echo "=========================================="
    cd "$PROJECT_DIR"
    docker compose -f "$COMPOSE_FILE" ps
    ;;

  logs)
    echo "=========================================="
    echo "  查看日志 (Ctrl+C 退出)"
    echo "=========================================="
    cd "$PROJECT_DIR"
    docker compose -f "$COMPOSE_FILE" logs -f
    ;;

  *)
    echo "用法: $0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac

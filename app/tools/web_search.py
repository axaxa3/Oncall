
import json
from agents.mcp import MCPServerStreamableHttp
from langchain_core.tools import tool
from loguru import logger

# 阿里云百炼 API Key
ALIYUN_API_KEY = "sk-0324247576e04c2faa25fa2b582b2a9f"

async def async_mcp_search(query: str):
    """异步调用 MCP 搜索（必须 await）"""
    search_mcp = MCPServerStreamableHttp(
        name="search_mcp",
        params={
            "url": "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp",
            "headers": {"Authorization": f"Bearer {ALIYUN_API_KEY}"},
            "timeout": 15,
        },
        max_retry_attempts=2
    )

    try:
        # 全部 await！这是你报错的核心原因
        await search_mcp.connect()
        result = await search_mcp.call_tool(
            tool_name="bailian_web_search",
            arguments={"query": query, "count": 5}
        )
        return result

    except Exception as e:
        logger.error(f"MCP 搜索失败: {e}")
        return None
    finally:
        await search_mcp.cleanup()

@tool
async def web_search_mcp(query: str) -> list:
    """
联网搜索工具，用于获取实时、时效性、动态变化、本地知识库无法覆盖的信息。
必须调用场景：实时天气、股票/基金/汇率行情、最新新闻、政策公告、赛事结果、商品价格、
交通信息、企业动态、考试通知、热点榜单、版本更新等一切随时间变化的外部信息。
当信息不确定是否过时、是否准确时，优先调用此工具。

Args:
    query: 搜索查询词
Returns:
    list: 搜索结果
"""
    try:
        # await 调用异步搜索
        result = await async_mcp_search(query)

        if not result or not hasattr(result, "content"):
            logger.warning("搜索返回空结果")
            return []

        # 解析返回结果
        text = result.content[0].text
        web_data = json.loads(text)
        pages = web_data.get("pages", [])

        logger.success(f"搜索成功，返回 {len(pages)} 条结果")
        return pages

    except json.JSONDecodeError:
        logger.error("搜索结果 JSON 解析失败")
        return []
    except Exception as e:
        logger.error(f"搜索工具异常: {str(e)}")
        return []
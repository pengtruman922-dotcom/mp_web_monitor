"""Function calling tool schemas for the Agent."""

TOOL_BROWSE_PAGE = {
    "type": "function",
    "function": {
        "name": "browse_page",
        "description": "使用浏览器打开指定URL的网页，返回页面的文本内容。用于浏览政府网站的首页、栏目页或文章详情页。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要访问的网页URL",
                }
            },
            "required": ["url"],
        },
    },
}

TOOL_DOWNLOAD_FILE = {
    "type": "function",
    "function": {
        "name": "download_file",
        "description": "下载指定URL的文件（PDF、DOC、DOCX、XLSX等格式）到本地。返回本地文件路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "文件的下载URL",
                },
                "filename": {
                    "type": "string",
                    "description": "保存的文件名（含扩展名，如 policy.pdf）",
                },
            },
            "required": ["url", "filename"],
        },
    },
}

TOOL_READ_DOCUMENT = {
    "type": "function",
    "function": {
        "name": "read_document",
        "description": "读取已下载的本地文件（PDF/DOC/DOCX/XLSX），提取其中的文本内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "本地文件的路径",
                }
            },
            "required": ["file_path"],
        },
    },
}

TOOL_SAVE_RESULT = {
    "type": "function",
    "function": {
        "name": "save_result",
        "description": "保存一条采集到的内容结果。每发现一条有价值的新内容（新闻、政策、通知等）都应调用此工具保存。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "内容标题",
                },
                "url": {
                    "type": "string",
                    "description": "内容的原文链接",
                },
                "content_type": {
                    "type": "string",
                    "enum": ["news", "policy", "notice", "file"],
                    "description": "内容类型：news(新闻)、policy(政策法规)、notice(通知公告)、file(文件)",
                },
                "summary": {
                    "type": "string",
                    "description": "内容摘要。从详情页采集时写200-500字；从列表页采集时可简短（一句话概括或复述标题）",
                },
                "published_date": {
                    "type": "string",
                    "description": "发布日期，格式 YYYY-MM-DD（如无法确定可为空字符串）",
                },
                "has_attachment": {
                    "type": "boolean",
                    "description": "是否包含附件",
                },
                "attachment_name": {
                    "type": "string",
                    "description": "附件文件名（如无附件可为空字符串）",
                },
                "attachment_type": {
                    "type": "string",
                    "description": "附件格式：pdf/doc/docx/xlsx（如无附件可为空字符串）",
                },
                "attachment_path": {
                    "type": "string",
                    "description": "附件的本地存储路径（如无附件可为空字符串）",
                },
                "attachment_summary": {
                    "type": "string",
                    "description": "附件内容的摘要（如无附件可为空字符串）",
                },
            },
            "required": ["title", "url", "content_type", "summary"],
        },
    },
}

TOOL_SAVE_RESULTS_BATCH = {
    "type": "function",
    "function": {
        "name": "save_results_batch",
        "description": "批量保存多条采集结果（一次保存多条，高效）。参数为一个JSON数组字符串。",
        "parameters": {
            "type": "object",
            "properties": {
                "items_json": {
                    "type": "string",
                    "description": '要保存的内容，JSON数组字符串。每个元素包含 title, url, published_date, content_type(news/policy/notice/file), summary。示例: [{"title":"标题1","url":"http://...","published_date":"2026-01-30","content_type":"news","summary":"摘要"}]',
                }
            },
            "required": ["items_json"],
        },
    },
}

TOOL_FINISH = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "标记当前监控源的采集工作已完成。当你认为已经充分浏览了网站并收集了所有本周新增内容后，调用此工具结束采集。",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "对本次采集的简要总结（发现了多少条内容、主要涉及哪些方面）",
                }
            },
            "required": ["summary"],
        },
    },
}

ALL_TOOLS = [
    TOOL_BROWSE_PAGE,
    TOOL_DOWNLOAD_FILE,
    TOOL_READ_DOCUMENT,
    TOOL_SAVE_RESULT,
    TOOL_SAVE_RESULTS_BATCH,
    TOOL_FINISH,
]

# Simplified tool set for section-level crawler sub-agents (Phase 1b)
CRAWLER_TOOLS = [
    TOOL_BROWSE_PAGE,
    TOOL_SAVE_RESULTS_BATCH,
    TOOL_SAVE_RESULT,
    TOOL_FINISH,
]

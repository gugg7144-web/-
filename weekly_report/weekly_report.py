import os
import json
import requests
from datetime import datetime, timedelta

# ==================== 1. 参数设置 (已填充您的信息) ====================
YUNXIAO_TOKEN = os.getenv("YUNXIAO_TOKEN", "pt-J1oh7LaBqfOGblP7nXiCReaN_d6ed378b-f856-4f3b-a0b0-5aa45954a91b")
ORGANIZATION_ID = os.getenv("ORGANIZATION_ID", "624580543add99e4db45a523")
TARGET_PROJECT = os.getenv("TARGET_PROJECT", "简单购软件")
TARGET_ASSIGNEE = os.getenv("TARGET_ASSIGNEE", "谷元璋")
BOT_WEBHOOK = os.getenv("BOT_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/63ff6aa9-7b39-40da-8467-2973a620b750")

# ==================== 2. 状态映射配置 ====================
# 已完成
STATUS_COMPLETED = ["已完成"]

# 已取消 / 终止
STATUS_CANCELLED = ["已拒绝", "暂不支持", "已取消"]

# ==================== 3. 云效 API 接口调用 ====================
def get_headers():
    return {
        "x-yunxiao-token": YUNXIAO_TOKEN,
        "Content-Type": "application/json"
    }

def fetch_project_space_id():
    """
    通过 SearchProjects 接口自动查找目标的 spaceId（云效 API 检索工作项必须提供 spaceId）
    """
    url = f"https://openapi-rdc.aliyuncs.com/oapi/v1/projex/organizations/{ORGANIZATION_ID}/projects:search"
    payload = {"page": 1, "perPage": 100}
    try:
        res = requests.post(url, headers=get_headers(), json=payload, timeout=10)
        if res.status_code == 200:
            projects = res.json()
            if isinstance(projects, list):
                for proj in projects:
                    proj_name = proj.get("name", "")
                    if TARGET_PROJECT in proj_name:
                        proj_id = proj.get("id")
                        print(f"✅ 成功找到项目 [{proj_name}]，获取到 spaceId: {proj_id}")
                        return proj_id
        print(f"⚠️ 组织内未匹配到名称包含 [{TARGET_PROJECT}] 的项目。")
    except Exception as e:
        print(f"❌ 查询项目 spaceId 异常: {e}")
    return None

def fetch_recent_workitems():
    """
    遍历云效常见的开放 API 路由节点，自动匹配可调用的接口并获取工作项数据
    """
    # 1. 动态获取 spaceId（解决 400 错误的关键）
    space_id = fetch_project_space_id()
    if not space_id:
        print("⚠️ 未能获取到有效的 spaceId，取消需求检索。")
        return []

    candidate_urls = [
        f"https://openapi-rdc.aliyuncs.com/oapi/v1/projex/organizations/{ORGANIZATION_ID}/workitems:search",
        f"https://devops.aliyun.com/oapi/v1/projex/organizations/{ORGANIZATION_ID}/workitems:search",
        f"https://openapi-rdc.aliyuncs.com/oapi/v1/projex/workitems:search",
        f"https://devops.aliyun.com/oapi/v1/projex/workitems:search"
    ]
    
    payload = {
        "category": "Req",
        "spaceId": space_id,
        "spaceType": "Project",
        "page": 1,
        "perPage": 100
    }

    raw_items = []
    success_url = None

    for url in candidate_urls:
        try:
            response = requests.post(url, headers=get_headers(), json=payload, timeout=10)
            if response.status_code == 200:
                res_data = response.json()
                if isinstance(res_data, list):
                    raw_items = res_data
                elif isinstance(res_data, dict):
                    raw_items = res_data.get("workitems") or res_data.get("result") or res_data.get("data") or []
                success_url = url
                print(f"✅ 成功连接云效 API 节点: {url}")
                break
            else:
                print(f"ℹ️ 尝试节点 [{url}] 状态码: {response.status_code}")
        except Exception as e:
            print(f"ℹ️ 尝试节点 [{url}] 连接异常: {e}")

    if not success_url and not raw_items:
        print("⚠️ 候选 API 节点均未打通，请检查 Token 权限或网络通畅性。")

    # 客户端精确过滤
    filtered_items = []
    seven_days_ago = datetime.now() - timedelta(days=7)

    for item in raw_items:
        # 获取空间/项目名称
        space_name = (item.get("space") or {}).get("name", "")
        # 获取负责人名称
        assigned_to = (item.get("assignedTo") or {}).get("name", "")
        # 获取修改时间或创建时间
        gmt_modified_str = item.get("gmtModified") or item.get("gmtCreate") or ""

        # 匹配逻辑
        match_project = (not TARGET_PROJECT) or (TARGET_PROJECT in space_name)
        match_assignee = (not TARGET_ASSIGNEE) or (TARGET_ASSIGNEE in assigned_to)
        
        # 7 天内时间判定
        match_time = True
        if gmt_modified_str:
            try:
                clean_time_str = gmt_modified_str.replace("T", " ")[:19]
                item_time = datetime.strptime(clean_time_str, "%Y-%m-%d %H:%M:%S")
                if item_time < seven_days_ago:
                    match_time = False
            except Exception:
                pass

        if match_project and match_assignee and match_time:
            filtered_items.append(item)
            
    return filtered_items

# ==================== 4. 数据整理与 Markdown 生成 ====================
def generate_weekly_markdown(workitems):
    """
    按需求流转状态归类生成周报
    """
    completed_items = []
    in_progress_items = []
    cancelled_items = []

    for item in workitems:
        title = item.get("subject", "未命名需求")
        status = (item.get("status") or {}).get("name", "未知状态")
        item_id = item.get("id", "") or item.get("identifier", "")
        url = f"https://devops.aliyun.com/workitem/{item_id}" if item_id else "#"
        
        item_line = f"• [{title}]({url})"

        if status in STATUS_COMPLETED:
            completed_items.append(item_line)
        elif status in STATUS_CANCELLED:
            cancelled_items.append(f"{item_line}（状态: {status}）")
        else:
            in_progress_items.append(f"{item_line}（当前节点: **{status}**）")

    # 动态生成 7 天时间范围说明
    end_date_str = datetime.now().strftime("%m月%d日")
    start_date_str = (datetime.now() - timedelta(days=7)).strftime("%m月%d日")

    md_content = f"🗓 **统计区间**：{start_date_str} ~ {end_date_str}\n\n"

    # 1. 已完成
    md_content += "✅ **【已完成需求】**\n"
    if completed_items:
        md_content += "\n".join(completed_items) + "\n\n"
    else:
        md_content += "• 本周暂无已完成需求\n\n"

    # 2. 进行中
    md_content += "🔄 **【推进中的需求】**\n"
    if in_progress_items:
        md_content += "\n".join(in_progress_items) + "\n\n"
    else:
        md_content += "• 当前无推进中的需求\n\n"

    # 3. 终止/已取消（若有）
    if cancelled_items:
        md_content += "🚫 **【终止/已取消需求】**\n"
        md_content += "\n".join(cancelled_items) + "\n\n"

    return md_content

# ==================== 5. 飞书卡片消息推送 ====================
def send_feishu_card(markdown_text, count):
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📊 【简单购软件】需求进度周报 ({today_str})"
                },
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": markdown_text
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"👤 负责人：谷元璋 | 📈 本周更新需求共 {count} 项 | 🤖 自动化推送"
                        }
                    ]
                }
            ]
        }
    }
    
    try:
        res = requests.post(BOT_WEBHOOK, json=payload, timeout=10)
        res_json = res.json()
        if res_json.get("code") == 0 or res_json.get("StatusCode") == 0:
            print("✅ 飞书卡片周报发送成功！")
        else:
            print(f"⚠️ 飞书推送返回异常: {res_json}")
    except Exception as e:
        print(f"❌ 飞书推送失败: {e}")

# ==================== 主入口 ====================
if __name__ == "__main__":
    print("🚀 开始检索云效【简单购软件】项目（负责人：谷元璋）的需求数据...")
    items = fetch_recent_workitems()
    print(f"📦 共检索到 {len(items)} 条符合条件的需求数据。")
    
    report_md = generate_weekly_markdown(items)
    
    print("📤 正向飞书群推送周报卡片...")
    send_feishu_card(report_md, len(items))
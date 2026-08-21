import os
import json
import requests
import urllib3
from datetime import datetime, timedelta
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 禁用未验证 HTTPS 请求警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 1. 参数设置 ====================
YUNXIAO_TOKEN = os.getenv("YUNXIAO_TOKEN", "pt-J1oh7LaBqfOGblP7nXiCReaN_d6ed378b-f856-4f3b-a0b0-5aa45954a91b")
ORGANIZATION_ID = os.getenv("ORGANIZATION_ID", "624580543add99e4db45a523")
TARGET_PROJECT = os.getenv("TARGET_PROJECT", "简单购软件")
TARGET_ASSIGNEE = os.getenv("TARGET_ASSIGNEE", "谷元璋")
BOT_WEBHOOK = os.getenv("BOT_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/63ff6aa9-7b39-40da-8467-2973a620b750")

# ==================== 2. 状态映射配置 ====================
CREATE_TIME_CHECK_STATUSES = ["待立项", "待设计"]  # 移除 "待产品内审"
STATUS_COMPLETED = ["已完成", "已发布", "已上线"]
STATUS_CANCELLED = ["已拒绝", "暂不支持", "已取消", "已终止"]

# ==================== 3. 工具函数与 API 接口调用 ====================
def parse_yunxiao_time(val):
    if not val:
        return None
    try:
        if isinstance(val, (int, float)):
            ts = val / 1000.0 if val > 1e11 else float(val)
            return datetime.fromtimestamp(ts)
        if isinstance(val, str) and val.isdigit():
            v = float(val)
            ts = v / 1000.0 if v > 1e11 else v
            return datetime.fromtimestamp(ts)
        if isinstance(val, str):
            clean_str = val.replace("T", " ")[:19]
            return datetime.strptime(clean_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None

def get_headers():
    return {
        "x-yunxiao-token": YUNXIAO_TOKEN,
        "Content-Type": "application/json"
    }

def fetch_project_space_id():
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

    raw_items = []
    
    for url in candidate_urls:
        page = 1
        url_success = False
        fetched_for_this_url = []

        while page <= 10:
            payload = {
                "category": "Req",
                "spaceId": space_id,
                "spaceType": "Project",
                "page": page,
                "perPage": 100
            }
            try:
                response = requests.post(url, headers=get_headers(), json=payload, timeout=10)
                if response.status_code == 200:
                    res_data = response.json()
                    items_page = []
                    if isinstance(res_data, list):
                        items_page = res_data
                    elif isinstance(res_data, dict):
                        items_page = res_data.get("workitems") or res_data.get("result") or res_data.get("data") or []

                    if not items_page:
                        break

                    fetched_for_this_url.extend(items_page)
                    url_success = True

                    if len(items_page) < 100:
                        break
                    
                    page += 1
                else:
                    break
            except Exception:
                break

        if url_success and fetched_for_this_url:
            print(f"✅ 成功连接云效 API 节点: {url}，共拉取到原始需求数据 {len(fetched_for_this_url)} 条")
            raw_items = fetched_for_this_url
            break

    if not raw_items:
        print("⚠️ 未获取到需求数据，请检查网络或 Token 权限。")

    filtered_items = []
    seven_days_ago = datetime.now() - timedelta(days=7)

    for item in raw_items:
        space_name = (item.get("space") or {}).get("name", "")
        assigned_to = (item.get("assignedTo") or {}).get("name", "")
        status_name = (item.get("status") or {}).get("name", "")

        match_project = (not TARGET_PROJECT) or (TARGET_PROJECT in space_name)
        match_assignee = (not TARGET_ASSIGNEE) or (TARGET_ASSIGNEE in assigned_to)

        if not (match_project and match_assignee):
            continue

        create_dt = parse_yunxiao_time(item.get("gmtCreate"))
        modify_dt = parse_yunxiao_time(item.get("gmtModified")) or create_dt

        match_time = False

        # --- 核心修改逻辑 ---
        if status_name == "待产品内审":
            # 【待产品内审】全量保留，不受时间限制
            match_time = True
        elif status_name in CREATE_TIME_CHECK_STATUSES:
            # 【待立项】、【待设计】只校验近 7 天创建
            if create_dt and create_dt >= seven_days_ago:
                match_time = True
        elif status_name in STATUS_COMPLETED or status_name in STATUS_CANCELLED:
            # 【已完成】、【已取消】校验近 7 天修改
            if modify_dt and modify_dt >= seven_days_ago:
                match_time = True
        else:
            # 其它推进中的状态（如开发中、测试中等）默认保留
            match_time = True

        if match_time:
            filtered_items.append(item)

    return filtered_items

# ==================== 4. 数据整理与 Markdown 生成 ====================
def generate_weekly_markdown(workitems):
    completed_items = []
    cancelled_items = []
    in_progress_map = defaultdict(list)

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
            in_progress_map[status].append(item_line)

    now = datetime.now()
    start_date_str = (now - timedelta(days=7)).strftime("%m月%d日")
    end_date_str = now.strftime("%m月%d日")

    md_content = f"🗓 **统计区间**：{start_date_str} ~ {end_date_str}\n\n"

    md_content += "✅ **【已完成需求】**\n"
    if completed_items:
        md_content += "\n".join(completed_items) + "\n\n"
    else:
        md_content += "• 本周暂无已完成需求\n\n"

    md_content += "🔄 **【推进中的需求】**\n"
    if in_progress_map:
        for status_node, items in in_progress_map.items():
            md_content += f"📌 **{status_node}** ({len(items)})\n"
            md_content += "\n".join(items) + "\n"
        md_content += "\n"
    else:
        md_content += "• 当前无推进中的需求\n\n"

    if cancelled_items:
        md_content += "🚫 **【终止/已取消需求】**\n"
        md_content += "\n".join(cancelled_items) + "\n\n"

    return md_content

# ==================== 5. 飞书卡片消息推送 (已增加 SSL 重试机制) ====================
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
                            "content": f"👤 负责人：谷元璋 | 📈 本周包含需求共 {count} 项 | 🤖 自动化推送"
                        }
                    ]
                }
            ]
        }
    }

    # 创建带有重试机制和自定义请求头的 Session
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    try:
        # 尝试标准 HTTPS 请求
        res = session.post(BOT_WEBHOOK, json=payload, headers=headers, timeout=15)
        res_json = res.json()
        if res_json.get("code") == 0 or res_json.get("StatusCode") == 0:
            print("✅ 飞书卡片周报发送成功！")
        else:
            print(f"⚠️ 飞书推送返回异常: {res_json}")
    except Exception as e:
        print(f"⚠️ 第一次推送遇 SSL 网络波动，尝试使用非严格模式重新发送... 错误细节: {e}")
        try:
            # 降级容错：禁用 verify 以解决 GitHub Actions 节点的 SSL EOF 报错
            res = requests.post(BOT_WEBHOOK, json=payload, headers=headers, timeout=15, verify=False)
            res_json = res.json()
            if res_json.get("code") == 0 or res_json.get("StatusCode") == 0:
                print("✅ 飞书卡片周报（容错模式）发送成功！")
            else:
                print(f"⚠️ 飞书推送返回异常: {res_json}")
        except Exception as ex:
            print(f"❌ 飞书推送最终失败: {ex}")

# ==================== 主入口 ====================
if __name__ == "__main__":
    print("🚀 开始检索云效【简单购软件】项目（负责人：谷元璋）的需求数据...")
    items = fetch_recent_workitems()
    print(f"📦 共检索到 {len(items)} 条符合条件的需求数据。")

    report_md = generate_weekly_markdown(items)

    print("📤 正在向飞书群推送周报卡片...")
    send_feishu_card(report_md, len(items))

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

# ==================== 1. 【多人周报配置，在这里增加/删除人员】 ====================
PERSON_CONFIGS = [
    {
        "name": "谷元璋",
        "YUNXIAO_TOKEN": "pt-J1oh7LaBqfOGblP7nXiCReaN_d6ed378b-f856-4f3b-a0b0-5aa45954a91b",
        "ORGANIZATION_ID": "624580543add99e4db45a523",
        "TARGET_PROJECT": "简单购软件",
        "TARGET_ASSIGNEE": "谷元璋",
        "BOT_WEBHOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/63ff6aa9-7b39-40da-8467-2973a620b750"
    },
    {
        "name": "秦巧丽",
        "YUNXIAO_TOKEN": "pt-zzG7HswTyiqRLQkCzlxPiG6C_03a9cac5-94fa-4c2d-b822-2bd0a810616d",
        "ORGANIZATION_ID": "624580543add99e4db45a523",
        "TARGET_PROJECT": "简单购软件",
        "TARGET_ASSIGNEE": "秦巧丽",
        "BOT_WEBHOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/745a6f96-9f41-44f6-b990-7d5ab416e1ed"
    },
    {
        "name": "贾晨阳",
        "YUNXIAO_TOKEN": "pt-YqnPlpZGFm8j7afrxtPBK5Fq_fff863b3-3ceb-4c63-b8e6-bdc3693268c3",
        "ORGANIZATION_ID": "624580543add99e4db45a523",
        "TARGET_PROJECT": "简单购软件",
        "TARGET_ASSIGNEE": "贾晨阳",
        "BOT_WEBHOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/65900b0e-3f79-42dc-9d3e-7d6d801d97ff"
    }
]

# ==================== 2. 状态映射与排序优先级配置 ====================
CREATE_TIME_CHECK_STATUSES = ["待立项", "待设计"]
STATUS_COMPLETED = ["已完成", "已发布", "已上线"]
STATUS_CANCELLED = ["已拒绝", "暂不支持", "已取消", "已终止"]

# 定义状态展示的优先级（按需求推进生命周期顺序排序）
# 补全了“待用例评审”、“进行中”、“研发中”等中间节点
STATUS_ORDER = [
    "待产品内审",
    "待立项",
    "待设计",
    "待研发评审",
    "待用例评审",
    "待确认",
    "待排期",
    "待开发",
    "开发中",
    "进行中",
    "研发中",
    "待联调",
    "待测试",
    "测试中",
    "待上线",
    "已上线",
    "已完成",
    "已发布",
    "已拒绝",
    "暂不支持",
    "已取消",
    "已终止"
]

def get_status_sort_key(item):
    """获取需求状态的排序 Key，保证同状态归类聚拢"""
    status_name = (item.get("status") or {}).get("name", "未知状态")
    if status_name in STATUS_ORDER:
        return (0, STATUS_ORDER.index(status_name))
    return (1, status_name)

# ==================== 3. 工具函数与 API 接口调用 ====================
def parse_yunxiao_time(val):
    """解析云效返回的各种格式时间字符串或毫秒时间戳"""
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
    """按项目名称搜索获取对应 SpaceId"""
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
    """获取指定负责人/创建人符合条件的需求条目"""
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

        while page <= 50:
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
        
        # 多字段兼容提取：获取负责人与创建人信息
        assigned_to_obj = item.get("assignedTo") or {}
        assigned_to_name = assigned_to_obj.get("name", "") if isinstance(assigned_to_obj, dict) else str(assigned_to_obj or "")
        if not assigned_to_name:
            assigned_to_name = item.get("assignedToName") or ""

        creator_obj = item.get("creator") or {}
        creator_name = creator_obj.get("name", "") if isinstance(creator_obj, dict) else str(creator_obj or "")
        if not creator_name:
            creator_name = item.get("creatorName") or ""

        status_name = (item.get("status") or {}).get("name", "")

        match_project = (not TARGET_PROJECT) or (TARGET_PROJECT in space_name)
        
        # 核心筛选逻辑：只要是“需求创建人”包含目标人员，或者“当前负责人”包含目标人员，均纳入该成员周报
        match_person = (not TARGET_ASSIGNEE) or (TARGET_ASSIGNEE in assigned_to_name or TARGET_ASSIGNEE in creator_name)

        if not (match_project and match_person):
            continue

        create_dt = parse_yunxiao_time(item.get("gmtCreate"))

        # --- 业务筛选规则 ---
        if status_name == "待产品内审":
            match_time = True
        elif status_name in CREATE_TIME_CHECK_STATUSES:
            if create_dt and create_dt >= seven_days_ago:
                match_time = True
            else:
                match_time = False
        elif status_name in STATUS_COMPLETED or status_name in STATUS_CANCELLED:
            status_change_dt = (
                parse_yunxiao_time(item.get("updateStatusAt")) or 
                parse_yunxiao_time(item.get("gmtModified")) or 
                create_dt
            )
            if status_change_dt and status_change_dt >= seven_days_ago:
                match_time = True
            else:
                match_time = False
        else:
            # “开发中”、“测试中”、“待排期”等推进中节点无条件保留
            match_time = True

        if match_time:
            filtered_items.append(item)

    return filtered_items

# ==================== 4. 构造飞书 Schema 2.0 原生表格卡片 Payload ====================
def build_feishu_card_payload(workitems, assignee_name):
    """
    构建符合飞书卡片 Schema 2.0 规范的真网格表格 Payload
    表头字段：序号、项目名称、标题、网址、状态、负责人
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()
    start_date_str = (now - timedelta(days=7)).strftime("%m月%d日")
    end_date_str = now.strftime("%m月%d日")

    # 按状态优先级进行统一分组与排序
    sorted_workitems = sorted(workitems, key=get_status_sort_key)

    table_rows = []
    for idx, item in enumerate(sorted_workitems, 1):
        title = item.get("subject", "未命名需求")
        status = (item.get("status") or {}).get("name", "未知状态")
        item_id = item.get("id", "") or item.get("identifier", "")
        
        # 获取需求链接
        url = item.get("url") or item.get("webUrl") or (f"https://devops.aliyun.com/projex/workitem/{item_id}" if item_id else "#")
        
        # 获取真实空间项目名
        project_name = (item.get("space") or {}).get("name") or TARGET_PROJECT
        
        # 负责人展示逻辑：周报属于谁，负责人列统一展示谁
        person = assignee_name
        
        # 管道符与中括号转义防破坏 JSON/Markdown
        clean_title = title.replace("|", "丨").replace("\n", " ").strip()
        
        table_rows.append({
            "seq": str(idx),
            "project": project_name,
            "title": clean_title,
            "url": f"[{url}]({url})",
            "status": status,
            "assignee": person
        })

    elements = [
        {
            "tag": "markdown",
            "content": f"🗓 **统计区间**：{start_date_str} ~ {end_date_str}"
        }
    ]

    if table_rows:
        elements.append({
            "tag": "table",
            "page_size": 50,
            "columns": [
                {"name": "seq", "display_name": "序号", "data_type": "text", "width": "auto"},
                {"name": "project", "display_name": "项目名称", "data_type": "text", "width": "auto"},
                {"name": "title", "display_name": "标题", "data_type": "text", "width": "auto"},
                {"name": "url", "display_name": "网址", "data_type": "lark_md", "width": "auto"},
                {"name": "status", "display_name": "状态", "data_type": "text", "width": "auto"},
                {"name": "assignee", "display_name": "负责人", "data_type": "text", "width": "auto"}
            ],
            "rows": table_rows
        })
    else:
        elements.append({
            "tag": "markdown",
            "content": "🎉 本周暂无符合条件的需求更新。"
        })

    elements.append({"tag": "hr"})
    # 使用 Schema 2.0 兼容的 markdown 浅灰色文本块替代废弃的 note 标签
    elements.append({
        "tag": "markdown",
        "content": f"<font color='grey'>👤 负责人：{assignee_name} | 📈 本周包含需求共 {len(workitems)} 项 | 🤖 自动化推送</font>"
    })

    payload = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📊 【{TARGET_PROJECT}】需求进度周报 ({today_str})"
                },
                "template": "blue"
            },
            "body": {
                "elements": elements
            }
        }
    }
    return payload

# ==================== 5. 飞书卡片消息推送 (带 SSL 重试与容错机制) ====================
def send_feishu_card(workitems, assignee_name, webhook_url):
    payload = build_feishu_card_payload(workitems, assignee_name)

    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    try:
        res = session.post(webhook_url, json=payload, headers=headers, timeout=15)
        res_json = res.json()
        if res_json.get("code") == 0 or res_json.get("StatusCode") == 0:
            print(f"✅ [{assignee_name}] 飞书网格表格周报卡片发送成功！")
        else:
            print(f"⚠️ [{assignee_name}] 飞书推送返回异常: {res_json}")
    except Exception as e:
        print(f"⚠️ [{assignee_name}] 第一次推送遇 SSL 网络波动，尝试使用容错模式重新发送... 错误细节: {e}")
        try:
            res = requests.post(webhook_url, json=payload, headers=headers, timeout=15, verify=False)
            res_json = res.json()
            if res_json.get("code") == 0 or res_json.get("StatusCode") == 0:
                print(f"✅ [{assignee_name}] 飞书网格表格周报卡片（容错模式）发送成功！")
            else:
                print(f"⚠️ [{assignee_name}] 飞书推送返回异常: {res_json}")
        except Exception as ex:
            print(f"❌ [{assignee_name}] 飞书推送最终失败: {ex}")

# ==================== 主入口：循环遍历每一个人 ====================
if __name__ == "__main__":
    for person_cfg in PERSON_CONFIGS:
        print("\n" + "="*60)
        print(f"🚀 开始处理【{person_cfg['name']}】的周报任务")
        print("="*60)

        # 动态覆盖全局变量，给底层函数使用
        global YUNXIAO_TOKEN, ORGANIZATION_ID, TARGET_PROJECT, TARGET_ASSIGNEE, BOT_WEBHOOK
        YUNXIAO_TOKEN = person_cfg["YUNXIAO_TOKEN"]
        ORGANIZATION_ID = person_cfg["ORGANIZATION_ID"]
        TARGET_PROJECT = person_cfg["TARGET_PROJECT"]
        TARGET_ASSIGNEE = person_cfg["TARGET_ASSIGNEE"]
        BOT_WEBHOOK = person_cfg["BOT_WEBHOOK"]

        # 拉取该负责人需求
        items = fetch_recent_workitems()
        print(f"📦 [{person_cfg['name']}] 共检索到 {len(items)} 条符合条件的需求数据。")

        # 发送对应 webhook 网格表格周报卡片
        print(f"📤 正在向【{person_cfg['name']}】飞书群推送网格表格周报卡片...")
        send_feishu_card(items, person_cfg["name"], person_cfg["BOT_WEBHOOK"])

    print("\n🎉 全部人员周报任务执行完毕！")

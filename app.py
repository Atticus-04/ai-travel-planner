import streamlit as st
import uuid
import json
import copy
import traceback
from datetime import datetime
import re
from urllib.parse import quote
import requests
from openai import OpenAI
import folium
from streamlit_folium import st_folium

# ==========================================
# 0. 全局系统配置与样式注入
# ==========================================
try:
    API_KEY = st.secrets["API_KEY"]
    BASE_URL = st.secrets.get("BASE_URL", "https://api.deepseek.com")
    MODEL_NAME = st.secrets.get("MODEL_NAME", "deepseek-chat")
    TAVILY_API_KEY = st.secrets.get("TAVILY_API_KEY", "")
except KeyError:
    st.error("未找到 API_KEY，请确保配置了 `.streamlit/secrets.toml`")
    st.stop()

st.set_page_config(page_title="定制化行程规划系统", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    h1 { font-size: 1.8rem !important; margin-bottom: 0.2rem !important; }
    h2 { font-size: 1.35rem !important; }
    h3 { font-size: 1.1rem !important; margin-top: 0.2rem !important; }
    .activity-card {
        background-color: rgba(128, 128, 128, 0.08);
        border-left: 4px solid #00b4d8;
        border-radius: 6px;
        padding: 10px 14px;
        margin-bottom: 10px;
        font-size: 0.9rem;
    }
    .hotel-card {
        border-left: 4px solid #f28482;
    }
    .tag-hotel {
        display: inline-block;
        background-color: rgba(242, 132, 130, 0.18);
        color: #d64545;
        padding: 1px 6px;
        border-radius: 4px;
        font-size: 0.78rem;
        margin-right: 6px;
        font-weight: 600;
    }
    .transit-badge {
        display: inline-block;
        background-color: rgba(0, 180, 216, 0.12);
        color: #0077b6;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 0.82rem;
        margin: 2px 0 10px 12px;
        font-weight: 500;
    }
    .error-banner {
        background-color: rgba(239, 68, 68, 0.1);
        border: 1px solid rgba(239, 68, 68, 0.35);
        color: #dc2626;
        border-radius: 6px;
        padding: 8px 14px;
        font-size: 0.92rem;
        font-weight: 500;
    }
    .ref-section {
        margin-top: 2rem;
        padding-top: 0.8rem;
        border-top: 1px solid rgba(128, 128, 128, 0.2);
        font-size: 0.76rem;
        color: #888888;
        line-height: 1.6;
    }
    .ref-section a {
        color: #777777;
        text-decoration: none;
    }
    .ref-section a:hover {
        text-decoration: underline;
        color: #0096c7;
    }
    /* 开发者隐形排错按钮样式 */
    div[data-testid="stElementContainer"]:has(.dev-trigger-marker) + div[data-testid="stElementContainer"] button {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        color: rgba(128, 128, 128, 0.15) !important;
        padding: 0 4px !important;
        min-height: 36px !important;
    }
    div[data-testid="stElementContainer"]:has(.dev-trigger-marker) + div[data-testid="stElementContainer"] button:hover {
        color: rgba(128, 128, 128, 0.7) !important;
        background: rgba(128, 128, 128, 0.08) !important;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 1. 日期解析与智能检索/大模型工具函数
# ==========================================
BLOCK_KEYWORDS = [
    "财报", "股票", "涨停", "跌停", "证券", "基金", "业绩预告",
    "招聘", "招标", "政府采购", "公告", "持股", "研报", "季度报告"
]

def parse_days_from_date_str(date_str):
    text = date_str.strip()
    if not text:
        return None

    now_year = datetime.now().year

    m_full = re.search(
        r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?\s*[-~至到]+\s*(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?",
        text
    )
    if m_full:
        y1, mo1, d1, y2, mo2, d2 = m_full.groups()
        y1 = int(y1) if y1 else now_year
        y2 = int(y2) if y2 else y1
        try:
            start_dt = datetime(y1, int(mo1), int(d1))
            end_dt = datetime(y2, int(mo2), int(d2))
            if end_dt < start_dt and not m_full.group(4):
                end_dt = datetime(y1 + 1, int(mo2), int(d2))
            calc_days = (end_dt - start_dt).days + 1
            return calc_days if 1 <= calc_days <= 30 else None
        except ValueError:
            return None

    m_short = re.search(
        r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?\s*[-~至到]+\s*(\d{1,2})日?",
        text
    )
    if m_short:
        y1, mo1, d1, d2 = m_short.groups()
        y1 = int(y1) if y1 else now_year
        try:
            start_dt = datetime(y1, int(mo1), int(d1))
            end_dt = datetime(y1, int(mo1), int(d2))
            calc_days = (end_dt - start_dt).days + 1
            return calc_days if 1 <= calc_days <= 30 else None
        except ValueError:
            return None

    return None

def sync_days_from_date():
    calc_days = parse_days_from_date_str(st.session_state.get("init_travel_date", ""))
    if calc_days is not None:
        st.session_state["init_days"] = calc_days

def plan_search_strategy(destination, user_input, days=3, travel_date=""):
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    date_hint = f"出行时间：{travel_date}" if travel_date else "出行时间：未指定"
    try:
        res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是旅游信息检索策略专家。请根据目的地、天数、出行时间和用户需求，返回纯 JSON：\n"
                        "{\n"
                        '  "guide_query": "15字以内，最适合搜真实游记/美食/避坑的中文搜索词",\n'
                        '  "event_query": "15字以内，用于搜该目的地在该时间段的限时展览/美食节/市集/节庆活动词（若无明确时间则搜近期热门展览活动）",\n'
                        '  "max_results": 5\n'
                        "}\n"
                        "说明：max_results 在 3 到 8 之间动态决定——天数多或小众目的地取 6-8，短途热门取 3-5。"
                    )
                },
                {
                    "role": "user",
                    "content": f"目的地：{destination}\n天数：{days}天\n{date_hint}\n用户需求：{user_input}"
                }
            ],
            temperature=0.5,
            response_format={"type": "json_object"},
            max_tokens=150
        )
        strategy = json.loads(res.choices[0].message.content)
        guide_q = strategy.get("guide_query", f"{destination} 旅游攻略 美食 避坑")
        event_q = strategy.get("event_query", f"{destination} {travel_date} 限时展览 市集 活动".strip())
        max_k = int(strategy.get("max_results", min(max(days + 2, 4), 7)))
        max_k = min(max(max_k, 3), 8)
        return guide_q, event_q, max_k
    except Exception:
        return f"{destination} 旅游攻略 美食 避坑", f"{destination} {travel_date} 展览 美食节 活动".strip(), 5

def _tavily_query(query, max_results=5, time_range="year"):
    if not TAVILY_API_KEY:
        return []
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": TAVILY_API_KEY.strip(),
        "query": query,
        "search_depth": "basic",
        "time_range": time_range,
        "include_answer": False,
        "max_results": max_results
    }
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    return response.json().get("results", [])

def _filter_results(results):
    cleaned = []
    seen_urls = set()
    for r in results:
        url = r.get("url", "")
        title = r.get("title", "")
        content = r.get("content", "")
        score = r.get("score", 0.5)

        if not url or url in seen_urls or score < 0.35:
            continue
        if any(bad in f"{title} {content}" for bad in BLOCK_KEYWORDS):
            continue

        seen_urls.add(url)
        cleaned.append(r)
    return cleaned

def fetch_social_guides(destination, user_input, days=3, travel_date="", search_events=True):
    if not TAVILY_API_KEY:
        return "无实时检索数据", "无限时活动数据", []

    guide_q, event_q, max_k = plan_search_strategy(destination, user_input, days, travel_date)
    sources = []
    guide_context = ""
    event_context = ""

    try:
        raw_guides = _tavily_query(guide_q, max_results=max_k, time_range="year")
        valid_guides = _filter_results(raw_guides)
        guide_context = f"关于【{guide_q}】的高质量网络攻略参考：\n\n"
        for r in valid_guides:
            title = r.get("title", "未命名攻略")
            guide_context += f"- 标题：{title}\n  摘要：{r.get('content', '')}\n\n"
            sources.append({"title": title, "url": r.get("url", "")})
    except Exception:
        guide_context = "主攻略实时检索失败"

    if search_events:
        try:
            raw_events = _tavily_query(event_q, max_results=3, time_range="month" if not travel_date else "year")
            valid_events = _filter_results(raw_events)
            if valid_events:
                event_context = f"关于【{event_q}】的限时活动/展览/节庆检索结果：\n\n"
                for r in valid_events:
                    title = r.get("title", "未命名活动")
                    event_context += f"- 标题：{title}\n  摘要：{r.get('content', '')}\n\n"
                    if not any(s["url"] == r.get("url", "") for s in sources):
                        sources.append({"title": title, "url": r.get("url", "")})
        except Exception:
            event_context = ""

    if sources:
        st.toast(f"已检索「{guide_q}」，筛选出 {len(sources)} 篇有效参考")
    return guide_context, event_context, sources

def chat_with_agent(messages, destination="目的地", days=3, travel_date="", search_events=True):
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

    user_latest_input = messages[-1]["content"] if messages else ""
    guide_context, event_context, sources = fetch_social_guides(
        destination, user_latest_input, days=days, travel_date=travel_date, search_events=search_events
    )

    system_message = {
        "role": "system",
        "content": (
            "你是一个结合了真实社群体验的定制化旅游规划引擎。\n"
            "【空间聚类原则】：每天的行程在地理位置上必须高度紧凑，绝不将相邻地点拆到不同天，绝不折返。\n"
            "【强制住宿闭环】：每一天 activities 列表的第1个节点必须是『酒店出发』，最后1个节点必须是『返回/入住酒店』。\n"
            "【文风与深度要求】：绝对禁止只用一句话敷衍，且所有输出文本中严禁使用任何 Emoji 表情符号，保持专业、克制、干货满满的编辑风格。每个景点必须提供具体怎么玩、最佳拍照打卡机位、周边地道美食推荐、以及网友真实评价或避坑注意事项。\n"
            "【限时活动提取规则】：\n"
            "- 如果检索结果中确实提到了与出行时间匹配的限时活动（如持续数周的美食节、特展、市集、当地节庆），请将其整理到 JSON 顶层的 `optional_events` 数组中作为附加选项，**不要**直接塞进 `days` 行程里（除非用户明确要求加入）。\n"
            "- 如果没有检索到明确、靠谱的限时活动，`optional_events` 必须直接返回空数组 `[]`，严禁编造凑数。\n"
            "【最高优先指令】：必须且只能输出一个合法的 JSON 对象，禁止输出任何 Markdown 或解释性废话。\n"
            "输出格式严格如下：\n"
            "{\n"
            '  "summary": "2-3句话总结本版行程的核心亮点、住宿策略与整体节奏",\n'
            '  "optional_events": [\n'
            "    {\n"
            '      "name": "限时活动名称",\n'
            '      "time_range": "活动时间范围",\n'
            '      "location": "举办地点",\n'
            '      "description": "1-2句话说明看点与推荐理由"\n'
            "    }\n"
            "  ],\n"
            '  "days": [\n'
            "    {\n"
            '      "day": 1,\n'
            '      "title": "第一天主题",\n'
            '      "activities": [\n'
            "        {\n"
            '          "time": "10:30",\n'
            '          "location": "某某区域酒店 (出发)",\n'
            '          "description": "简要说明出发准备",\n'
            '          "highlights": "无",\n'
            '          "food_tips": "酒店周边早餐推荐（若有）",\n'
            '          "tips": "出行前注意事项",\n'
            '          "transit_to_next": "地铁2号线直达（约15分钟）",\n'
            '          "lat": 31.2304, \n'
            '          "lon": 121.4737 \n'
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
        )
    }

    api_messages = messages.copy()
    if api_messages and api_messages[-1]["role"] == "user":
        original_user_input = api_messages[-1]["content"]
        fortified_input = (
            f"用户具体要求：{original_user_input}\n\n"
            f"【实时网络攻略参考】：\n{guide_context}\n\n"
            f"【限时活动检索参考】：\n{event_context if event_context else '无'}\n\n"
            "【终极系统要求】：\n"
            "1. 仅返回纯 JSON，直接以 { 开头，文本中禁止出现任何 Emoji！\n"
            "2. 每天 activities 首尾必须是『酒店』节点！\n"
            "3. 每个景点必须填满 description、highlights、food_tips、tips 四个字段！\n"
            "4. 若无限时活动，optional_events 必须为 []。"
        )
        if days > 7:
            fortified_input += "\n5. 本次行程天数较多，请将每个景点的各字段控制在1句精炼干货以内，确保完整输出全部天数的合法 JSON，切勿截断。"
        api_messages[-1] = {"role": "user", "content": fortified_input}

    final_messages = [system_message] + api_messages

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=final_messages,
        temperature=0.3,
        max_tokens=8192,
        response_format={"type": "json_object"}
    )

    if response.choices[0].finish_reason == "length":
        raise ValueError("模型输出超过 max_tokens=8192 长度限制，JSON 被截断。")

    return response.choices[0].message.content, sources

# ==========================================
# 1.5 版本级配套推荐生成器
# ==========================================
def extract_itinerary_brief(parsed_data):
    days_list = parsed_data.get("days", [])
    spots, hotels, day_briefs = [], [], []
    for d in days_list:
        d_spots = []
        for act in d.get("activities", []):
            loc = act.get("location", "")
            if "酒店" in loc or "住宿" in loc:
                hotels.append(loc)
            elif loc:
                spots.append(loc)
                d_spots.append(f"{act.get('time', '')} {loc}")
        day_briefs.append(f"Day {d.get('day')}: " + " -> ".join(d_spots))
    return list(dict.fromkeys(spots)), list(dict.fromkeys(hotels)), "\n".join(day_briefs)

def generate_logistics_module(module_type, parsed_data, session_info, custom_origin=None):
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    dest = session_info.get("destination", "上海")
    origin = (custom_origin if custom_origin is not None else session_info.get("origin", "")).strip()
    travel_date = session_info.get("travel_date", "")
    spots, hotels, brief_text = extract_itinerary_brief(parsed_data)

    if module_type == "hotel":
        prompt = (
            f"目的地：{dest}。当前版本行程安排如下：\n{brief_text}\n"
            f"当前行程参考住宿：{', '.join(hotels) if hotels else '未指定'}。\n"
            "请结合该版本行程的地理分布，推荐 3 个最合适或不同风格的住宿片区/代表性酒店（例如：交通枢纽型、商圈夜生活型、高性价比型），"
            "严禁使用 Emoji，仅返回纯 JSON：\n"
            "{\n"
            '  "hotels": [\n'
            "    {\n"
            '      "name": "片区与推荐酒店示例（如：静安寺商圈 · 全季/亚朵）",\n'
            '      "price_range": "参考价位（如：350-550元/晚）",\n'
            '      "pros": "结合本版行程说明选址优势",\n'
            '      "cons": "潜在缺点或注意事项"\n'
            "    }\n"
            "  ]\n"
            "}"
        )
        res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        return json.loads(res.choices[0].message.content)

    elif module_type == "intercity":
        search_ctx = ""
        if origin and TAVILY_API_KEY:
            try:
                raw = _tavily_query(f"{origin} 到 {dest} 高铁 航班 耗时 票价 推荐", max_results=4, time_range="year")
                for r in _filter_results(raw):
                    search_ctx += f"- {r.get('title')}: {r.get('content')}\n"
            except Exception:
                pass
        prompt = (
            f"请为从【{origin}】往返【{dest}】的旅客提供跨城交通推荐。出行时间：{travel_date or '未指定'}。\n"
            f"当前版本首尾行程概览：\n{brief_text}\n"
            f"实时检索参考：\n{search_ctx or '无'}\n"
            "要求（严禁使用 Emoji，输出简洁清晰的 Markdown）：\n"
            "1. 对比高铁/动车与航班的耗时、大致票价区间与优劣，给出明确推荐。\n"
            "2. 结合本版 Day 1 和最后一天的行程节奏，建议具体的去程抵达时段和返程出发时段（以及推荐选哪个车站/机场更顺路）。\n"
            "3. 给出 1-2 条购票省钱或防踩坑建议。"
        )
        res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return res.choices[0].message.content

    elif module_type == "reservations":
        search_ctx = ""
        if spots and TAVILY_API_KEY:
            try:
                raw = _tavily_query(f"{dest} {' '.join(spots[:4])} 门票 预约 公众号 学生票 避坑", max_results=4, time_range="year")
                for r in _filter_results(raw):
                    search_ctx += f"- {r.get('title')}: {r.get('content')}\n"
            except Exception:
                pass
        prompt = (
            f"目的地：{dest}。当前版本包含以下景点/场所：{', '.join(spots)}。\n"
            f"实时检索参考：\n{search_ctx or '无'}\n"
            "请筛选出其中**需要提前预约、抢票或购买门票**的地点（免费且随到随进的开放街区可省略），严禁使用 Emoji，仅返回纯 JSON：\n"
            "{\n"
            '  "items": [\n'
            "    {\n"
            '      "spot": "景点名称",\n'
            '      "channel": "预约/购票渠道（如：官方微信公众号、微信小程序、携程/美团等）",\n'
            '      "timing": "提前几天、是否需要踩点抢票及放票时间",\n'
            '      "cost_discount": "收费情况及学生/老人/儿童优惠政策",\n'
            '      "notes": "其他关键注意事项（如带身份证、闭馆日等）"\n'
            "    }\n"
            "  ]\n"
            "}"
        )
        res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        return json.loads(res.choices[0].message.content)

    elif module_type == "local_transit":
        prompt = (
            f"目的地：{dest}。当前版本行程概览：\n{brief_text}\n"
            "请总结本版行程在当地出行的交通注意事项（严禁使用 Emoji，输出精炼 Markdown）：\n"
            "1. 地铁/公交乘车方式（如推荐的官方 App、支付宝/微信乘车码或手机 NFC 交通卡）。\n"
            "2. 是否需要购买特殊票种（如地铁一日/三日票、机场快线、景区接驳车、轮渡等）以及是否划算。\n"
            "3. 结合本版行程中的特殊点位（如远郊景点、夜间打车难区域、限行等）给出避坑提示。如果没有特殊注意事项，简短说明即可，不要凑字数。"
        )
        res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return res.choices[0].message.content

# ==========================================
# 2. 状态初始化、错误提示与撤回机制
# ==========================================
if "sessions" not in st.session_state:
    st.session_state.sessions = {}
if "current_session_id" not in st.session_state:
    st.session_state.current_session_id = None
if "last_error" not in st.session_state:
    st.session_state.last_error = None
if "show_error_detail" not in st.session_state:
    st.session_state.show_error_detail = False
if "confirm_del_session" not in st.session_state:
    st.session_state.confirm_del_session = None
if "confirm_del_ver" not in st.session_state:
    st.session_state.confirm_del_ver = None

def create_new_session():
    new_id = str(uuid.uuid4())
    st.session_state.sessions[new_id] = {
        "title": f"新行程 ({datetime.now().strftime('%H:%M')})",
        "origin": "",
        "destination": "上海",
        "days": 5,
        "travel_date": "",
        "messages": [],
        "versions": [],
        "active_version_id": None
    }
    st.session_state.current_session_id = new_id
    st.session_state.last_error = None
    st.session_state.show_error_detail = False

def record_error(err_detail):
    st.session_state.last_error = err_detail
    st.session_state.show_error_detail = False

def render_error_banner():
    """统一友好报错栏 + 撤回按钮 + 开发者隐形排错按钮"""
    if not st.session_state.get("last_error"):
        return

    c_msg, c_dev, c_undo = st.columns([9, 0.6, 2.2])
    with c_msg:
        st.markdown(
            '<div class="error-banner">出现错误，请稍后重试</div>',
            unsafe_allow_html=True
        )
    with c_dev:
        # 开发者隐形按钮（悬停时微微可见）
        st.markdown('<span class="dev-trigger-marker"></span>', unsafe_allow_html=True)
        if st.button("·", key="dev_error_toggle", help="开发者排错信息"):
            st.session_state.show_error_detail = not st.session_state.show_error_detail
            st.rerun()
    with c_undo:
        if st.button("撤回到上一步", key="btn_undo_error", use_container_width=True):
            st.session_state.last_error = None
            st.session_state.show_error_detail = False
            st.rerun()

    if st.session_state.show_error_detail:
        st.code(st.session_state.last_error, language="python")

if not st.session_state.sessions:
    create_new_session()

current_session = st.session_state.sessions[st.session_state.current_session_id]

# ==========================================
# 3. 侧边栏：行程会话管理（含删除二次确认）
# ==========================================
with st.sidebar:
    st.header("行程管理")
    if st.button("+ 新建行程", type="primary", use_container_width=True):
        create_new_session()
        st.rerun()

    st.markdown("### 历史行程")
    for session_id, session_data in list(st.session_state.sessions.items()):
        col1, col2 = st.columns([4, 1])
        with col1:
            is_active = (session_id == st.session_state.current_session_id)
            btn_label = f"▸ {session_data['title']}" if is_active else session_data['title']
            if st.button(btn_label, key=f"btn_{session_id}", use_container_width=True):
                st.session_state.current_session_id = session_id
                st.session_state.confirm_del_session = None
                st.session_state.last_error = None
                st.rerun()
        with col2:
            if st.button("×", key=f"del_{session_id}", help="删除行程"):
                st.session_state.confirm_del_session = session_id
                st.rerun()

        # 删除行程二次确认
        if st.session_state.confirm_del_session == session_id:
            st.caption("确认删除该行程？")
            cc1, cc2 = st.columns(2)
            with cc1:
                if st.button("确认删除", key=f"ok_del_s_{session_id}", type="primary", use_container_width=True):
                    del st.session_state.sessions[session_id]
                    st.session_state.confirm_del_session = None
                    if st.session_state.current_session_id == session_id:
                        if st.session_state.sessions:
                            st.session_state.current_session_id = list(st.session_state.sessions.keys())[0]
                        else:
                            create_new_session()
                    st.rerun()
            with cc2:
                if st.button("取消", key=f"no_del_s_{session_id}", use_container_width=True):
                    st.session_state.confirm_del_session = None
                    st.rerun()

# ==========================================
# 4. 核心视图：单版本分栏渲染 + 版本专属配套按钮
# ==========================================
def render_trip_workspace(version_obj):
    plan_json_str = version_obj["json_str"]
    ver_id = version_obj["id"]
    triggered_prompt = None
    triggered_label = None

    if "logistics" not in version_obj:
        version_obj["logistics"] = {}

    cleaned_str = plan_json_str.strip()
    if cleaned_str.startswith("```"):
        cleaned_str = re.sub(r"^```(?:json)?", "", cleaned_str)
        cleaned_str = re.sub(r"```$", "", cleaned_str)
    cleaned_str = cleaned_str.strip()

    try:
        data = json.loads(cleaned_str)
    except Exception:
        record_error(f"JSON 解析失败：\n{traceback.format_exc()}\n\n原始输出：\n{plan_json_str}")
        st.rerun()
        return None, None

    with st.expander(f"📌 本版需求与总览 · {version_obj['label']}", expanded=True):
        st.caption(f"需求指令：{version_obj['prompt']}")
        st.markdown(f"**行程总结**：{data.get('summary', '暂无总结')}")

    # 版本专属：订票与出行配套推荐按钮栏
    panel_key = f"active_panel_{ver_id}"
    if panel_key not in st.session_state:
        st.session_state[panel_key] = None

    b1, b2, b3, b4 = st.columns(4)
    panel_defs = [
        ("hotel", "酒店推荐与换址", b1),
        ("intercity", "跨城交通推荐", b2),
        ("reservations", "景点预约清单", b3),
        ("local_transit", "当地交通指南", b4),
    ]
    for p_id, p_label, col in panel_defs:
        with col:
            is_open = (st.session_state[panel_key] == p_id)
            if st.button(
                p_label,
                key=f"btn_log_{ver_id}_{p_id}",
                type="primary" if is_open else "secondary",
                use_container_width=True
            ):
                st.session_state[panel_key] = None if is_open else p_id
                st.rerun()

    active_panel = st.session_state[panel_key]
    if active_panel:
        with st.container(border=True):
            try:
                if active_panel == "hotel":
                    st.markdown("##### 酒店推荐与换址重排（基于本版行程）")
                    if "hotel" not in version_obj["logistics"]:
                        with st.spinner("正在结合本版景点分布分析最佳住宿片区..."):
                            version_obj["logistics"]["hotel"] = generate_logistics_module("hotel", data, current_session)

                    hotels_data = version_obj["logistics"]["hotel"].get("hotels", [])
                    for h_idx, h in enumerate(hotels_data):
                        hc1, hc2 = st.columns([4, 1.3])
                        with hc1:
                            st.markdown(f"**{h.get('name', '推荐酒店')}**（{h.get('price_range', '价位待定')}）")
                            st.caption(f"优势：{h.get('pros', '')} ｜ 注意：{h.get('cons', '')}")
                        with hc2:
                            if st.button("改住这里并重排", key=f"switch_h_{ver_id}_{h_idx}", use_container_width=True):
                                triggered_prompt = (
                                    f"请将住宿更换为【{h.get('name')}】，并围绕该酒店位置重新优化每天的出发/返回路线、顺路顺序与交通方式。"
                                )
                                triggered_label = f"换住·{h.get('name', '')[:6]}"

                    st.markdown("---")
                    cc1, cc2 = st.columns([4, 1.3])
                    with cc1:
                        custom_hotel = st.text_input(
                            "自定义酒店",
                            placeholder="已有心仪酒店？输入名称或商圈（如：外滩华尔道夫 / 虹桥火车站附近）",
                            label_visibility="collapsed",
                            key=f"custom_hotel_{ver_id}"
                        )
                    with cc2:
                        if st.button("按此位置重排", key=f"apply_custom_h_{ver_id}", type="primary", use_container_width=True):
                            if custom_hotel.strip():
                                triggered_prompt = (
                                    f"请将住宿更换为【{custom_hotel.strip()}】，并围绕该酒店位置重新优化每天的出发/返回路线、顺路顺序与交通方式。"
                                )
                                triggered_label = f"换住·{custom_hotel.strip()[:6]}"
                            else:
                                st.warning("请先输入酒店名称或区域。")

                elif active_panel == "intercity":
                    st.markdown("##### 跨城往返交通推荐")
                    dest = current_session.get("destination", "上海")
                    origin = current_session.get("origin", "").strip()

                    if not origin:
                        ic1, ic2 = st.columns([3, 1])
                        with ic1:
                            input_origin = st.text_input(
                                "补充出发地",
                                placeholder="初始未填写出发地，请输入（如：北京、杭州）",
                                label_visibility="collapsed",
                                key=f"add_origin_{ver_id}"
                            )
                        with ic2:
                            if st.button("查询往返方案", key=f"query_ic_{ver_id}", type="primary", use_container_width=True):
                                if input_origin.strip():
                                    current_session["origin"] = input_origin.strip()
                                    version_obj["logistics"].pop("intercity", None)
                                    st.rerun()
                    else:
                        if "intercity" not in version_obj["logistics"]:
                            with st.spinner(f"正在检索 {origin} 往返 {dest} 的高铁与航班信息..."):
                                version_obj["logistics"]["intercity"] = generate_logistics_module("intercity", data, current_session)

                        st.markdown(version_obj["logistics"]["intercity"])

                        ctrip_flight = f"https://flights.ctrip.com/online/list/oneway-{quote(origin)}-{quote(dest)}"
                        ctrip_train = f"https://trains.ctrip.com/webapp/train/list?ticketType=0&dStation={quote(origin)}&aStation={quote(dest)}"
                        st.markdown(
                            f"**实时余票与价格直达**："
                            f"[携程机票查询]({ctrip_flight}) ｜ "
                            f"[携程火车票查询]({ctrip_train}) ｜ "
                            f"[铁路 12306 官网](https://www.12306.cn/index/)"
                        )

                elif active_panel == "reservations":
                    st.markdown("##### 本版景点预约与购票清单")
                    if "reservations" not in version_obj["logistics"]:
                        with st.spinner("正在核对本版景点的预约渠道、放票时间与优惠政策..."):
                            version_obj["logistics"]["reservations"] = generate_logistics_module("reservations", data, current_session)

                    items = version_obj["logistics"]["reservations"].get("items", [])
                    if items:
                        for it in items:
                            st.markdown(
                                f"**{it.get('spot', '')}** — 渠道：{it.get('channel', '')}  \n"
                                f"- 预约时间：{it.get('timing', '')}  \n"
                                f"- 费用与优惠：{it.get('cost_discount', '')}  \n"
                                f"- 注意事项：{it.get('notes', '')}"
                            )
                    else:
                        st.info("本版行程中的景点大多无需提前预约，随到随逛即可。")

                elif active_panel == "local_transit":
                    st.markdown("##### 当地交通与购票注意事项")
                    if "local_transit" not in version_obj["logistics"]:
                        with st.spinner("正在整理当地乘车与交通卡建议..."):
                            version_obj["logistics"]["local_transit"] = generate_logistics_module("local_transit", data, current_session)
                    st.markdown(version_obj["logistics"]["local_transit"])
            except Exception:
                st.session_state[panel_key] = None
                record_error(traceback.format_exc())
                st.rerun()

    # 日程切换与左右分栏视图
    days_data = data.get("days", [])
    if days_data:
        tab_titles = [f"Day {day['day']} · {day.get('title', '')}" for day in days_data]
        selected_title = st.radio(
            "选择日程",
            tab_titles,
            horizontal=True,
            label_visibility="collapsed",
            key=f"day_radio_{ver_id}"
        )
        i = tab_titles.index(selected_title)
        day_info = days_data[i]
        activities = day_info.get("activities", [])
        map_data = []

        st.markdown("---")
        col_list, col_map = st.columns([1.25, 1], gap="medium")

        with col_list:
            for idx, act in enumerate(activities):
                loc_name = act.get('location', '未定')
                is_hotel = "酒店" in loc_name or "住宿" in loc_name
                card_class = "activity-card hotel-card" if is_hotel else "activity-card"

                title_prefix = (
                    "<span class='tag-hotel'>住宿</span>"
                    if is_hotel
                    else f"<span style='color:#0096c7; margin-right:6px;'>{idx + 1:02d}</span>"
                )

                desc = act.get("description", "")
                highlights = act.get("highlights", "")
                food_tips = act.get("food_tips", "")
                tips = act.get("tips", "")

                details_html = f"<div style='margin-top:4px;'>{desc}</div>"
                if highlights and highlights != "无":
                    details_html += f"<div style='margin-top:4px;'><b>打卡亮点：</b>{highlights}</div>"
                if food_tips and food_tips != "无":
                    details_html += f"<div style='margin-top:4px;'><b>美食攻略：</b>{food_tips}</div>"
                if tips and tips != "无":
                    details_html += f"<div style='margin-top:4px; color:#b45309;'><b>评价与避坑：</b>{tips}</div>"

                st.markdown(f"""
                <div class="{card_class}">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span style="font-weight:bold; font-size:0.98rem;">{title_prefix}{loc_name}</span>
                        <span style="font-size:0.84rem; opacity:0.75;">{act.get('time', '全天')}</span>
                    </div>
                    {details_html}
                </div>
                """, unsafe_allow_html=True)

                transit = act.get("transit_to_next", "无")
                if transit and transit != "无" and idx < len(activities) - 1:
                    st.markdown(f"<div class='transit-badge'>↓ 交通：{transit}</div>", unsafe_allow_html=True)

                lat = act.get("lat")
                lon = act.get("lon")
                if lat is not None and lon is not None:
                    try:
                        map_data.append({
                            "lat": float(lat),
                            "lon": float(lon),
                            "name": loc_name
                        })
                    except ValueError:
                        pass

        with col_map:
            if map_data:
                st.markdown("🗺️ **当日动线图**（左上角 `+/-` 缩放，右上角可切换底图）")

                avg_lat = sum(d["lat"] for d in map_data) / len(map_data)
                avg_lon = sum(d["lon"] for d in map_data) / len(map_data)

                m = folium.Map(
                    location=[avg_lat, avg_lon],
                    zoom_start=13,
                    tiles=None,
                    scroll_wheel_zoom=False
                )

                # 默认使用高德中文底图，确保国内网络直连可用
                folium.TileLayer(
                    tiles="https://webrd02.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
                    attr="高德地图",
                    name="高德中文地图 (国内直连)",
                    show=True
                ).add_to(m)
                folium.TileLayer(tiles="OpenStreetMap", name="OpenStreetMap (需海外网络)", show=False).add_to(m)
                folium.TileLayer(tiles="CartoDB positron", name="极简浅色 (需海外网络)", show=False).add_to(m)

                route_coords = []
                for idx, point in enumerate(map_data):
                    coord = [point["lat"], point["lon"]]
                    route_coords.append(coord)

                    is_hotel = "酒店" in point['name'] or "住宿" in point['name']
                    bg_color = "#f28482" if is_hotel else "#00b4d8"

                    icon_html = f"""
                    <div style="
                        background-color: {bg_color};
                        color: white;
                        border-radius: 50%;
                        width: 24px;
                        height: 24px;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        font-weight: bold;
                        font-family: Arial;
                        border: 2px solid white;
                        box-shadow: 0 2px 4px rgba(0,0,0,0.3);
                        font-size: 12px;
                    ">{idx + 1}</div>
                    """

                    folium.Marker(
                        location=coord,
                        tooltip=f"{idx + 1}. {point['name']}",
                        icon=folium.DivIcon(html=icon_html, icon_anchor=(12, 12))
                    ).add_to(m)

                if len(route_coords) > 1:
                    folium.PolyLine(
                        locations=route_coords,
                        color="#00b4d8",
                        weight=4,
                        opacity=0.85,
                        tooltip="游览顺序"
                    ).add_to(m)

                lats = [c[0] for c in route_coords]
                lons = [c[1] for c in route_coords]
                min_lat, max_lat = min(lats), max(lats)
                min_lon, max_lon = min(lons), max(lons)

                lat_pad = max((max_lat - min_lat) * 0.2, 0.005)
                lon_pad = max((max_lon - min_lon) * 0.2, 0.005)

                m.fit_bounds([
                    [min_lat - lat_pad, min_lon - lon_pad],
                    [max_lat + lat_pad, max_lon + lon_pad]
                ])

                folium.LayerControl(position="topright").add_to(m)

                st_folium(
                    m,
                    use_container_width=True,
                    height=440,
                    returned_objects=[],
                    key=f"map_{ver_id}_day_{i}"
                )
            else:
                st.info("当日暂无有效地理坐标")

    # 可选限时活动推荐模块
    optional_events = data.get("optional_events", [])
    if optional_events:
        st.markdown("---")
        with st.expander(f"同期限时活动参考（发现 {len(optional_events)} 项，可勾选加入行程）", expanded=False):
            selected_events = []
            for e_idx, ev in enumerate(optional_events):
                ev_name = ev.get("name", "未命名活动")
                ev_time = ev.get("time_range", "时间待定")
                ev_loc = ev.get("location", "地点待定")
                ev_desc = ev.get("description", "")
                checked = st.checkbox(
                    f"**{ev_name}**（{ev_time} · {ev_loc}）— {ev_desc}",
                    key=f"ev_{ver_id}_{e_idx}"
                )
                if checked:
                    selected_events.append(f"{ev_name}（地点：{ev_loc}，时间：{ev_time}）")

            if st.button("将勾选活动融入行程并生成新版本", key=f"apply_ev_{ver_id}", type="primary"):
                if selected_events:
                    events_str = "；".join(selected_events)
                    triggered_prompt = f"请将以下限时活动按地理位置顺路原则，合理融入当前行程中：{events_str}。"
                    triggered_label = "加入限时活动"
                else:
                    st.warning("请先勾选至少一项活动。")

    # 页面最底部：论文参考文献风格展示
    sources = version_obj.get("sources", [])
    if sources:
        ref_items_html = "".join(
            f"<div>[{idx}] <a href='{src['url']}' target='_blank'>{src['title']}</a></div>"
            for idx, src in enumerate(sources, 1)
        )
        st.markdown(f"""
        <div class="ref-section">
            <div style="font-weight:600; margin-bottom:4px;">参考资料</div>
            {ref_items_html}
        </div>
        """, unsafe_allow_html=True)

    return triggered_prompt, triggered_label

# ==========================================
# 5. 辅助函数：提交新版本生成（带状态快照与出错自动撤回）
# ==========================================
def run_new_version(user_input, label_override=None, search_events=False):
    sid = st.session_state.current_session_id
    # 先保存操作前的完整快照，出错时自动撤回
    snapshot = copy.deepcopy(st.session_state.sessions[sid])
    st.session_state.last_error = None
    st.session_state.show_error_detail = False

    try:
        current_session["messages"].append({"role": "user", "content": user_input})
        with st.spinner("正在检索实时攻略并生成新版本方案..."):
            response, sources = chat_with_agent(
                current_session["messages"],
                destination=current_session.get("destination", "上海"),
                days=current_session.get("days", 5),
                travel_date=current_session.get("travel_date", ""),
                search_events=search_events
            )

            # 提前校验 JSON 合法性，防止把损坏的版本写入状态
            cleaned_check = response.strip()
            if cleaned_check.startswith("```"):
                cleaned_check = re.sub(r"^```(?:json)?", "", cleaned_check)
                cleaned_check = re.sub(r"```$", "", cleaned_check).strip()
            json.loads(cleaned_check)

            current_session["messages"].append({"role": "assistant", "content": response})

            ver_num = len(current_session["versions"]) + 1
            short_title = user_input[:8] + "..." if len(user_input) > 8 else user_input
            new_ver_id = str(uuid.uuid4())
            current_session["versions"].append({
                "id": new_ver_id,
                "label": label_override or f"V{ver_num} · {short_title}",
                "prompt": user_input,
                "json_str": response,
                "sources": sources,
                "logistics": {}
            })
            current_session["active_version_id"] = new_ver_id
    except Exception:
        # 出错自动回滚到操作前的状态
        st.session_state.sessions[sid] = snapshot
        record_error(traceback.format_exc())

    st.rerun()

# ==========================================
# 6. 主界面：初始表单 / 版本工作台
# ==========================================
st.title("定制化行程规划工作台")
render_error_banner()

if not current_session["versions"]:
    st.markdown("填写基础信息，系统将结合实时网络攻略为您生成第一版结构化行程。")
    st.markdown("---")

    if "init_days" not in st.session_state:
        st.session_state["init_days"] = 5
    if "init_travel_date" not in st.session_state:
        st.session_state["init_travel_date"] = ""

    col1, col2, col3, col4 = st.columns([1, 1, 0.8, 1.2])
    with col1:
        origin = st.text_input("出发地 (选填)", placeholder="如：北京")
    with col2:
        destination = st.text_input("目的地", value="上海")
    with col3:
        days = st.number_input("行程天数", min_value=1, max_value=30, step=1, key="init_days")
    with col4:
        travel_date = st.text_input(
            "出行时间 (选填)",
            placeholder="如：5月1日-5日 或 11月下旬",
            key="init_travel_date",
            on_change=sync_days_from_date
        )

    preference = st.text_area(
        "出行偏好 (选填)",
        value="大学生游玩，每天行程不要太早，但是晚上没有关系，可以安排夜生活的项目；需要有一天迪士尼，这一天可以早。",
        placeholder="可留空；或填写如：带父母出行节奏慢、喜欢小众博物馆、预算有限等"
    )

    if st.button("生成 V1 初始行程", type="primary"):
        dest_clean = destination.strip() if destination and destination.strip() else "上海"
        origin_clean = origin.strip() if origin else ""
        current_session["title"] = f"{dest_clean} {days}日游"
        current_session["origin"] = origin_clean
        current_session["destination"] = dest_clean
        current_session["days"] = days
        current_session["travel_date"] = travel_date.strip()

        origin_text = f"从 {origin_clean} 出发前往 " if origin_clean else "前往 "
        date_text = f"出行时间约为 {travel_date.strip()}。" if travel_date and travel_date.strip() else ""
        pref_text = (
            preference.strip()
            if preference and preference.strip()
            else "无特殊偏好，按经典热门且节奏舒适的路线安排"
        )

        initial_prompt = (
            f"请帮我规划 {origin_text}{dest_clean} 的 {days} 日行程。{date_text}"
            f"偏好：{pref_text}。如果涉及跨城交通，请在第一天和最后一天安排接驳。"
        )
        run_new_version(initial_prompt, label_override="V1 · 初始方案", search_events=True)

else:
    versions = current_session["versions"]

    st.markdown("##### 方案版本记录（点击切换对比）")
    ver_cols = st.columns(min(len(versions), 5) if len(versions) > 0 else 1)

    for idx, ver in enumerate(versions):
        col = ver_cols[idx % len(ver_cols)]
        with col:
            c1, c2 = st.columns([4, 1])
            is_selected = (ver["id"] == current_session["active_version_id"])
            btn_type = "primary" if is_selected else "secondary"
            with c1:
                if st.button(ver["label"], key=f"sel_ver_{ver['id']}", type=btn_type, use_container_width=True):
                    current_session["active_version_id"] = ver["id"]
                    st.session_state.confirm_del_ver = None
                    st.rerun()
            with c2:
                if st.button("×", key=f"del_ver_{ver['id']}", help="删除此版本"):
                    st.session_state.confirm_del_ver = ver["id"]
                    st.rerun()

            # 删除版本二次确认
            if st.session_state.confirm_del_ver == ver["id"]:
                vc1, vc2 = st.columns(2)
                with vc1:
                    if st.button("确认删", key=f"ok_del_v_{ver['id']}", type="primary", use_container_width=True):
                        current_session["versions"] = [v for v in versions if v["id"] != ver["id"]]
                        st.session_state.confirm_del_ver = None
                        if current_session["active_version_id"] == ver["id"]:
                            if current_session["versions"]:
                                current_session["active_version_id"] = current_session["versions"][-1]["id"]
                            else:
                                current_session["active_version_id"] = None
                                current_session["messages"] = []
                        st.rerun()
                with vc2:
                    if st.button("取消", key=f"no_del_v_{ver['id']}", use_container_width=True):
                        st.session_state.confirm_del_ver = None
                        st.rerun()

    st.markdown("---")

    active_ver = next((v for v in current_session["versions"] if v["id"] == current_session["active_version_id"]), None)
    trig_prompt, trig_label = (None, None)
    if active_ver:
        trig_prompt, trig_label = render_trip_workspace(active_ver)

    if trig_prompt:
        ver_num = len(versions) + 1
        final_label = f"V{ver_num} · {trig_label}" if trig_label else None
        run_new_version(trig_prompt, label_override=final_label, search_events=False)

    if user_input := st.chat_input("输入修改要求以生成新版本，例如：'把第三天的晚餐换成地道本帮菜，并增加一个爵士酒吧'"):
        run_new_version(user_input, search_events=False)
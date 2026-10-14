# -*- coding: utf-8 -*-
"""
秀动艺人「新演出上新」监控脚本（独立运行，可部署到 GitHub Actions / 云函数 / 常开设备）

功能：
  1. 抓取指定艺人的秀动 PC 艺人页（服务端渲染 HTML），解析每场演出
  2. 与本地状态文件对比，找出【本次新增】的演出
  3. 有新增 → 推送钉钉机器人 markdown 提醒，并更新状态；无新增 → 仅更新状态
  4. 首次运行只建立基线（记录当前全部演出），不推送

状态持久化：
  - GitHub Actions：状态文件 showstart_state.json 通过 git 提交回仓库，天然去重
  - 云函数 / 常开设备：状态文件保存在脚本同目录，需保证目录可写且持久

依赖：仅标准库（urllib / json / hmac / hashlib / base64 / re / time / os），无需安装第三方包。
运行：python showstart_monitor.py
环境变量（钉钉机器人）：DING_WEBHOOK、DING_SECRET
"""
import os
import re
import time
import json
import base64
import hashlib
import hmac
import urllib.parse
import urllib.request

# ---------------- 配置 ----------------
# 艺人列表：(页面显示名, 秀动艺人ID)
ARTISTS = [
    ("周乘羽", "17794095"),
    ("虎哥", "16807321"),
    ("龙飘飘", "8803371"),
    ("出来玩", "27693764"),
    ("艾斯凯瑞", "5435782"),
    ("极品贵公子", "3693572"),
    ("三火", "9860422"),
    ("mac", "3478074"),
    ("脆皮", "3668507"),
    ("topbarry", "8952275"),
    ("张方钊", "3424771"),
    ("Matt吕彦良", "7303871"),
    ("华云龙KLE", "4460540"),
    ("BOBBYNOPEACE", "9838654"),
    ("AIOZ", "3590193"),
    ("KEYI杨豪", "1612615"),
    ("泰格西", "9270197"),
    ("王齐铭", "1435722"),
    ("黄之仪Kyra Zilver", "21472695"),
    ("XMASwu (吴骜)", "4230349"),
    ("Mula sakee", "2967260"),
    ("THOME", "2967277"),
    ("罗言", "9255624"),
    ("Capper（张砚拙）", "592366"),
    ("kandi", "4013104"),
    ("法老Pharaoh", "339341"),
    ("艾志恒Asen", "4876767"),
    ("翁杰_Winjay", "4251211"),
    ("Rapeter吴嘉轩", "1642105"),
    ("小酷Coola", "1386641"),
    ("沧桑CangSang", "5630545"),
    ("李大奔", "636462"),
    ("Vansdaddy", "9395599"),
    ("SASIOVERLXRD", "6250524"),
    ("Vinz-T", "19653597"),
    ("谷江山V.R.M", "13787718"),
    ("高天佐Trouble.Z", "339335"),
    ("陈一豪Clear", "14051395"),
    ("Kc左元杰", "4162350"),
    ("G.G张思源", "362985"),
    ("邹沛沛", "17762099"),
    ("姜云升", "928639"),
    ("江辰", "2581035"),
    ("JinJiBeWater.隼", "3934337"),
    ("JOMO", "21039392"),
    ("连麻Swimming", "4083444"),
    ("谢帝", "18287"),
    ("YOUNG建坤（Y.O.U.N.G）", "1732729"),
    ("孟子坤", "845429"),
    ("GX高旭", "7335649"),
    ("h3R3刘清云", "1295435"),
    ("Sea Power", "21288657"),
    ("吴垚滔", "14447897"),
    ("小精灵", "936553"),
    ("李毅杰PISSY", "1869773"),
    ("蔡淇", "15336832"),
    ("张智涵", "2981947"),
    ("毛二", "19194925"),
    ("那奇沃夫", "4168141"),
    ("SHING02", "142705"),
    ("kkluv", "6402131"),
    ("功夫胖KUNGFU-PEN", "1677865"),
    ("李佳隆Jello Rio", "608826"),
    ("Bridge（布瑞吉）", "358746"),
    ("孙盛希 Shi Shi", "3120474"),
    ("GALI", "384482"),
]

# 状态文件（记录已见过的演出，用于去重）
STATE_FILE = os.environ.get("SHOWSTART_STATE_FILE", "showstart_state.json")

# 钉钉机器人凭据（从环境变量读取，不要把密钥硬编码）
DING_WEBHOOK = os.environ.get("DING_WEBHOOK", "")
DING_SECRET = os.environ.get("DING_SECRET", "")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
FETCH_TIMEOUT = 20
SLEEP_BETWEEN = 0.5  # 页面间隔，避免过快被限流


# ---------------- 工具函数 ----------------
def http_get(url: str) -> str:
    """抓取网页文本"""
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_html(x) -> str:
    """去掉 HTML 标签并压缩空白"""
    if not x:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).strip()


def parse_events(html: str) -> list:
    """从艺人页 HTML 解析演出列表。
    每个演出形如 <a href="/event/<id>"...><p class="name">标题</p>...<p class="price">价格</p><p class="time">日期</p><p class="addr">场馆</p></a>
    """
    events = []
    for m in re.finditer(r'<a href="/event/(\d+)"[^>]*>(.*?)</a>', html, re.S):
        eid = m.group(1)
        inner = m.group(2)
        name = re.search(r'class="name"[^>]*>(.*?)</p>', inner, re.S)
        price = re.search(r'class="price"[^>]*>.*?¥([\d]+)', inner, re.S)
        time_ = re.search(r'class="time"[^>]*>(.*?)</p>', inner, re.S)
        addr = re.search(r'class="addr"[^>]*>(.*?)</p>', inner, re.S)
        events.append({
            "id": eid,
            "title": clean_html(name.group(1) if name else ""),
            "price": price.group(1) if price else "",
            "date": clean_html(time_.group(1) if time_ else ""),
            "venue": clean_html(addr.group(1) if addr else ""),
        })
    return events


def ding_sign(secret: str) -> tuple:
    """钉钉自定义机器人签名。返回 (timestamp, sign)"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return timestamp, sign


def ding_send_markdown(text: str, title: str = "秀动新演出提醒") -> str:
    """推送 markdown 消息到钉钉。返回接口响应文本。"""
    if not (DING_WEBHOOK and DING_SECRET):
        return "未配置 DING_WEBHOOK / DING_SECRET，跳过推送"
    timestamp, sign = ding_sign(DING_SECRET)
    url = f"{DING_WEBHOOK}&timestamp={timestamp}&sign={sign}"
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen": []}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def build_markdown(new_events) -> str:
    """构造钉钉 markdown 文本"""
    lines = ["## 🎤 秀动新演出提醒", "", "以下艺人**有新的演出上新**：", ""]
    by_artist = {}
    for name, e in new_events:
        by_artist.setdefault(name, []).append(e)
    for name, evs in by_artist.items():
        lines.append(f"**{name}**：")
        for e in evs:
            price = f"¥{e['price']}起" if e["price"] else "票价待定"
            venue = f"@{e['venue']}" if e["venue"] else ""
            lines.append(f"- {e['title']}　{e['date']}　{venue}　{price}")
        lines.append("")
    lines.append("数据来源：秀动官网（自动监控）")
    return "\n".join(lines)


# ---------------- 主流程 ----------------
def main():
    first_run = not os.path.exists(STATE_FILE)
    state = load_state()
    seen = set(state["seen"])

    all_records = []   # (name, e)
    new_events = []    # (name, e)
    errors = []

    for name, aid in ARTISTS:
        url = f"https://www.showstart.com/artist/{aid}"
        try:
            html = http_get(url)
            events = parse_events(html)
            for e in events:
                all_records.append((name, e))
                key = f"{name}|{e['title']}|{e['date']}"
                if key not in seen:
                    new_events.append((name, e))
        except Exception as ex:
            errors.append(f"{name}(id={aid}): {ex}")
        time.sleep(SLEEP_BETWEEN)

    # 更新去重集合并持久化
    for name, e in all_records:
        seen.add(f"{name}|{e['title']}|{e['date']}")
    state["seen"] = sorted(seen)
    save_state(state)

    if first_run:
        print(f"首次运行，仅建立基线（共记录 {len(all_records)} 场演出），不推送。")
    elif new_events:
        resp = ding_send_markdown(build_markdown(new_events))
        print(f"检测到 {len(new_events)} 场新演出，钉钉推送结果: {resp}")
    else:
        print("本次无新增演出，状态已更新。")

    if errors:
        print("部分页面抓取失败:", "; ".join(errors))

    # 供外部判断成功与否
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())

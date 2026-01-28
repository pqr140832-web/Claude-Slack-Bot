from flask import Flask, request, jsonify
import requests
import re
from datetime import datetime, timezone, timedelta
import json
import os
import random
import threading
import time
import base64

app = Flask(__name__)

# ========== 配置 ==========
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
JSONBIN_API_KEY = os.environ.get("JSONBIN_API_KEY")
JSONBIN_USER_DATA = os.environ.get("JSONBIN_USER_DATA")
JSONBIN_SCHEDULES = os.environ.get("JSONBIN_SCHEDULES")
JSONBIN_MEMORIES = os.environ.get("JSONBIN_MEMORIES")
JSONBIN_CHAT_LOGS = os.environ.get("JSONBIN_CHAT_LOGS")

API_TOKEN_LIMITS = {
    "第三方sonnet": 110000,
    "sonnet": 190000,
    "opus": 190000,
    "haiku": 190000
}

APIS = {
    "第三方sonnet": {
        "url": os.environ.get("API_URL_1"),
        "key": os.environ.get("API_KEY_1"),
        "model": "[第三方逆1] claude-sonnet-4.5 [输出只有3~4k]",
        "vision": False,
        "cost": 1
    },
    "sonnet": {
        "url": os.environ.get("API_URL_1"),
        "key": os.environ.get("API_KEY_1"),
        "model": "claude-sonnet-4-5 [官逆1]",
        "vision": True,
        "cost": 4
    },
    "opus": {
        "url": os.environ.get("API_URL_2"),
        "key": os.environ.get("API_KEY_2"),
        "model": "福利-claude-opus-4-5",
        "vision": True,
        "cost": 2
    },
    "haiku": {
        "url": os.environ.get("API_URL_3"),
        "key": os.environ.get("API_KEY_3"),
        "model": "[code]claude-haiku-4-5-20251001",
        "vision": True,
        "cost": 1
    }
}

DEFAULT_API = "第三方sonnet"
UNLIMITED_USERS = ["sakuragochyan"]
POINTS_LIMIT = 20
MEMORY_LIMIT = 2000
CONVERSATION_TIMEOUT = 300

CN_TIMEZONE = timezone(timedelta(hours=8))

processed_events = set()
pending_messages = {}
pending_timers = {}
pending_clear_logs = {}

EMOJI_ALIASES = {
    "thumbs_up": "thumbsup",
    "thumb_up": "thumbsup",
    "+1": "thumbsup",
    "like": "thumbsup",
    "thumbs_down": "thumbsdown",
    "thumb_down": "thumbsdown",
    "-1": "thumbsdown",
    "dislike": "thumbsdown",
    "joy": "laughing",
    "sob": "cry",
    "crying": "cry",
    "sad": "cry",
    "love": "heart",
    "red_heart": "heart",
    "think": "thinking_face",
    "thinking": "thinking_face",
    "hmm": "thinking_face",
    "clapping": "clap",
    "applause": "clap",
    "party": "tada",
    "celebrate": "tada",
    "celebration": "tada",
    "stars": "sparkles",
    "glitter": "sparkles",
    "shine": "sparkles",
    "hi": "wave",
    "hello": "wave",
    "bye": "wave",
    "thanks": "pray",
    "thank_you": "pray",
    "please": "pray",
    "gratitude": "pray",
    "hundred": "100",
    "perfect": "100",
    "flame": "fire",
    "hot": "fire",
    "lit": "fire",
    "look": "eyes",
    "see": "eyes",
    "watching": "eyes",
    "ok": "ok_hand",
    "okay": "ok_hand",
    "strong": "muscle",
    "strength": "muscle",
    "flex": "muscle",
    "cool": "sunglasses",
    "check": "white_check_mark",
    "yes": "white_check_mark",
    "no": "x",
    "wrong": "x",
    "sleep": "zzz",
    "sleepy": "zzz",
    "tired": "zzz",
    "sweat": "sweat_smile",
    "nervous": "sweat_smile",
}

VALID_EMOJIS = [
    "heart", "thumbsup", "thumbsdown", "laughing", "cry", "fire", 
    "eyes", "thinking_face", "clap", "tada", "star", "wave", 
    "pray", "sparkles", "100", "rocket", "muscle", "ok_hand", 
    "raised_hands", "sunglasses", "white_check_mark", "x", "zzz", 
    "sweat_smile", "blush", "wink", "grin", "smile"
]

# ========== JSONBin 工具函数 ==========

def jsonbin_save(bin_id, data):
    try:
        requests.put(
            f"https://api.jsonbin.io/v3/b/{bin_id}",
            headers={
                "X-Master-Key": JSONBIN_API_KEY,
                "Content-Type": "application/json"
            },
            json=data,
            timeout=30
        )
    except Exception as e:
        print(f"JSONBin 保存失败: {e}")

def jsonbin_load(bin_id, default=None):
    try:
        resp = requests.get(
            f"https://api.jsonbin.io/v3/b/{bin_id}/latest",
            headers={"X-Master-Key": JSONBIN_API_KEY},
            timeout=30
        )
        if resp.status_code == 200:
            record = resp.json().get("record", default or {})
            if "init" in record:
                del record["init"]
            return record
    except Exception as e:
        print(f"JSONBin 读取失败: {e}")
    return default or {}

# ========== 时间工具 ==========

def get_cn_time():
    return datetime.now(CN_TIMEZONE)

def get_time_str():
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    now = get_cn_time()
    return now.strftime("%Y年%m月%d日 %H:%M:%S 星期") + weekdays[now.weekday()]

def get_time_period():
    hour = get_cn_time().hour
    if 5 <= hour < 9:
        return "早上", "早上好之类的问候"
    elif 9 <= hour < 12:
        return "上午", "上午的问候"
    elif 12 <= hour < 14:
        return "中午", "中午好、吃饭了吗之类的"
    elif 14 <= hour < 18:
        return "下午", "下午的问候"
    elif 18 <= hour < 22:
        return "晚上", "晚上好之类的"
    else:
        return "深夜", "注意休息之类的关心"

# ========== 数据持久化 ==========

def load_user_data():
    return jsonbin_load(JSONBIN_USER_DATA, {})

def save_user_data(data):
    jsonbin_save(JSONBIN_USER_DATA, data)

def load_schedules():
    return jsonbin_load(JSONBIN_SCHEDULES, {})

def save_schedules(data):
    jsonbin_save(JSONBIN_SCHEDULES, data)

# ========== 聊天记录 ==========

def load_chat_logs():
    return jsonbin_load(JSONBIN_CHAT_LOGS, {})

def save_chat_logs(data):
    jsonbin_save(JSONBIN_CHAT_LOGS, data)

def log_message(channel, role, content, username=None, model=None, is_reset=False, hidden=False):
    try:
        logs = load_chat_logs()
        if channel not in logs:
            logs[channel] = []
        
        timestamp = get_time_str()
        
        if is_reset:
            logs[channel].append({
                "type": "reset",
                "time": timestamp
            })
        else:
            entry = {
                "time": timestamp,
                "role": role,
                "content": content,
                "hidden": hidden
            }
            if role == "user":
                entry["username"] = username or "未知"
            else:
                entry["model"] = model or "未知"
            logs[channel].append(entry)
        
        save_chat_logs(logs)
    except Exception as e:
        print(f"log_message 出错: {e}")

def clear_chat_logs(channel):
    try:
        logs = load_chat_logs()
        logs[channel] = []
        save_chat_logs(logs)
    except Exception as e:
        print(f"clear_chat_logs 出错: {e}")

# ========== 记忆系统 ==========

def load_all_memories():
    return jsonbin_load(JSONBIN_MEMORIES, {})

def save_all_memories(data):
    jsonbin_save(JSONBIN_MEMORIES, data)

def load_memories(user_id):
    all_mem = load_all_memories()
    return all_mem.get(user_id, [])

def save_memories(user_id, memories):
    all_mem = load_all_memories()
    all_mem[user_id] = memories
    save_all_memories(all_mem)

def add_memory(user_id, content):
    memories = load_memories(user_id)
    total_chars = sum(len(m["content"]) for m in memories)

    while total_chars + len(content) > MEMORY_LIMIT and memories:
        removed = memories.pop(0)
        total_chars -= len(removed["content"])

    memories.append({
        "content": content,
        "time": get_time_str()
    })
    save_memories(user_id, memories)

def delete_memory(user_id, index):
    memories = load_memories(user_id)
    if 1 <= index <= len(memories):
        removed = memories.pop(index - 1)
        save_memories(user_id, memories)
        return removed["content"]
    return None

def clear_memories(user_id):
    save_memories(user_id, [])

def format_memories(user_id, show_numbers=True):
    memories = load_memories(user_id)
    if not memories:
        return ""

    lines = []
    for i, m in enumerate(memories, 1):
        if show_numbers:
            lines.append(f"{i}. {m['content']}")
        else:
            lines.append(f"• {m['content']}")
    return "\n".join(lines)

def get_channel_members(channel):
    try:
        resp = requests.get(
            "https://slack.com/api/conversations.members",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"channel": channel}
        )
        result = resp.json()
        if result.get("ok"):
            return result.get("members", [])
    except:
        pass
    return []

def get_all_memories_for_channel(channel):
    members = get_channel_members(channel)
    all_memories = []

    for member_id in members:
        mem = format_memories(member_id, show_numbers=False)
        if mem:
            display_name = get_display_name(member_id)
            all_memories.append(f"【{display_name}的记忆】\n{mem}")

    return "\n\n".join(all_memories) if all_memories else ""

def is_dm_channel(channel):
    return channel.startswith("D")

def get_channel_name(channel_id):
    if is_dm_channel(channel_id):
        return "私聊"
    try:
        resp = requests.get(
            "https://slack.com/api/conversations.info",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"channel": channel_id}
        )
        result = resp.json()
        if result.get("ok"):
            return "#" + result["channel"]["name"]
    except:
        pass
    return f"#{channel_id}"

def get_channel_id_by_name(name):
    name = name.lower().strip().lstrip('#')
    
    try:
        resp = requests.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"types": "public_channel,private_channel", "limit": 200}
        )
        result = resp.json()
        if result.get("ok"):
            for ch in result.get("channels", []):
                if ch["name"].lower() == name:
                    return ch["id"]
    except Exception as e:
        print(f"获取频道列表失败: {e}")
    
    return None

# ========== 历史记录管理 ==========

def estimate_tokens(text):
    if not text:
        return 0
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', str(text)))
    other_chars = len(str(text)) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)

def build_history_messages(user, current_channel, api_name):
    max_tokens = API_TOKEN_LIMITS.get(api_name, 100000)
    available_tokens = int(max_tokens * 0.7)
    
    current_is_dm = is_dm_channel(current_channel)
    current_scene_name = "私聊" if current_is_dm else get_channel_name(current_channel)
    
    dm_history = user.get("dm_history", [])
    channel_history = user.get("channel_history", [])
    last_channel = user.get("last_channel", "")
    last_channel_name = get_channel_name(last_channel) if last_channel else "#频道"
    
    tagged_history = []
    
    for i, m in enumerate(dm_history):
        # 过滤空消息
        if not m.get("content"):
            continue
        tagged_history.append({
            "role": m["role"],
            "content": m["content"],
            "scene": "私聊",
            "scene_tag": "[私聊]",
            "index": i * 2,
            "is_current": current_is_dm
        })
    
    for i, m in enumerate(channel_history):
        # 过滤空消息
        if not m.get("content"):
            continue
        tagged_history.append({
            "role": m["role"],
            "content": m["content"],
            "scene": "channel",
            "scene_tag": f"[{last_channel_name}]",
            "index": i * 2 + 1,
            "is_current": not current_is_dm
        })
    
    tagged_history.sort(key=lambda x: x["index"])
    
    total_tokens = sum(estimate_tokens(m["content"]) for m in tagged_history)
    
    while total_tokens > available_tokens and tagged_history:
        removed = tagged_history.pop(0)
        total_tokens -= estimate_tokens(removed["content"])
    
    messages = []
    
    for m in tagged_history:
        if m["is_current"]:
            content = m["content"]
        else:
            content = f"{m['scene_tag']} {m['content']}"
        
        messages.append({
            "role": m["role"],
            "content": content
        })
    
    return messages

# ========== 其他工具 ==========

def get_username(user_id):
    try:
        resp = requests.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"user": user_id}
        )
        result = resp.json()
        if result.get("ok"):
            return result["user"]["name"]
    except:
        pass
    return user_id

def get_display_name(user_id):
    try:
        resp = requests.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"user": user_id}
        )
        result = resp.json()
        if result.get("ok"):
            return result["user"]["real_name"] or result["user"]["name"]
    except:
        pass
    return user_id

def get_user_dm_channel(user_id):
    try:
        resp = requests.post(
            "https://slack.com/api/conversations.open",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json"
            },
            json={"users": user_id}
        )
        result = resp.json()
        if result.get("ok"):
            return result["channel"]["id"]
    except Exception as e:
        print(f"获取私聊频道失败: {e}")
    return None

def is_unlimited_user(user_id):
    username = get_username(user_id)
    return username in UNLIMITED_USERS

def check_and_use_points(user_id, api_name):
    if is_unlimited_user(user_id):
        return True, -1, None

    cost = APIS.get(api_name, {}).get("cost", 1)
    all_data = load_user_data()
    user = all_data.get(user_id, {})
    points_used = user.get("points_used", 0)
    remaining = POINTS_LIMIT - points_used

    if remaining < cost:
        return False, remaining, f"积分不足！剩余 {remaining} 分，{api_name} 需要 {cost} 分。"

    user["points_used"] = points_used + cost
    all_data[user_id] = user
    save_user_data(all_data)

    return True, POINTS_LIMIT - user["points_used"], None

def is_in_conversation(user_id, channel):
    all_data = load_user_data()
    user = all_data.get(user_id, {})
    
    channel_last_active = user.get("channel_last_active", {})
    last_active = channel_last_active.get(channel, 0)
    
    in_conv = (get_cn_time().timestamp() - last_active) < CONVERSATION_TIMEOUT
    print(f"[Debug] is_in_conversation: channel={channel}, last_active={last_active}, now={get_cn_time().timestamp()}, result={in_conv}")
    
    return in_conv

def get_system_prompt(mode="long", user_id=None, channel=None, msg_count=1):
    memories_text = ""
    if channel:
        if is_dm_channel(channel):
            if user_id:
                mem = format_memories(user_id, show_numbers=False)
                if mem:
                    display_name = get_display_name(user_id)
                    memories_text = f"\n\n【{display_name}的记忆】\n{mem}"
        else:
            mem = get_all_memories_for_channel(channel)
            if mem:
                memories_text = f"\n\n{mem}"

    current_scene = "私聊" if is_dm_channel(channel) else get_channel_name(channel)
    time_period, time_greeting = get_time_period()
    
    user_id_hint = ""
    if user_id:
        user_id_hint = f"\n当前对话用户的 ID 是：{user_id}（如需 @ 用户，使用 <@{user_id}>）"
    
    base = f"""你是一个友好的AI助手。
当前时间（中国时间）: {get_time_str()}
现在是{time_period}，说话时注意符合这个时间段（比如{time_greeting}）
当前场景：{current_scene}{user_id_hint}
{memories_text}

Slack 格式规则：
- 粗体：*文字*
- 斜体：_文字_
- 删除线：~文字~
- 代码：`代码` 或 ```代码块```
- 列表：• 或 1. 2. 3.
- 引用：> 开头
- @用户：<@用户ID>（例如 <@{user_id}>）

禁止：# 标题、LaTeX、Markdown 表格

===== 场景意识（极其重要！！！）=====
*你必须时刻注意当前对话发生在哪个场景！*

- 当前场景是：*{current_scene}*
- 历史记录中带 [私聊] 标签的是私聊中的对话
- 历史记录中带 [#频道名] 标签的是在该频道中的对话
- 没有标签的消息属于当前场景

*场景规则*：
1. 私聊是私密的！在频道里不要主动提起私聊的内容，除非用户主动说
2. 如果用户在频道里回复了你在私聊问的问题，这很奇怪，可以指出
3. 频道里所有人都能看到，说话要注意
4. 有些敏感话题建议私聊："这个我们私下聊？"

===== 你的特殊能力 =====

用 [[隐藏]] 包裹的内容不会发给用户，但你能看到：

1. *定时消息*（必须包含日期！）：
   [[定时|YYYY-MM-DD|HH:MM|提示内容]]
   例如：[[定时|2025-01-26|10:30|提醒用户开会]]
   也可以用来：想分享有趣的事、单纯想打招呼、任何你想说的话
   时间可以是任意的，不需要是整点

2. *每日消息*：
   [[每日|HH:MM|主题]]

3. *长期记忆*（每人限2000字）：
   [[记忆|用户ID|内容]] 或 [[记忆|内容]]（默认当前用户）

4. *特殊日期*（0:00触发）：
   [[特殊日期|MM-DD|描述]]

5. *跨场景发消息*：
   [[私聊|内容]] - 在频道时发私聊消息给用户
   [[发到频道|频道名|内容]] - 发消息到指定频道（可用：chat、general、random）
   例如：[[发到频道|chat|大家好！]]

6. *表情反应*：
   [[反应|emoji名称]] - 给用户的消息加表情
   使用场景：用户说了让你开心/感动/好笑的话、分享好消息、简单认可
   不要每条都加，偶尔用更自然
   可用：heart, thumbsup, thumbsdown, laughing, cry, fire, eyes, thinking_face, clap, tada, star, wave, pray, sparkles, 100, rocket, muscle, ok_hand, raised_hands, sunglasses, white_check_mark, x, zzz, sweat_smile, blush, wink, grin, smile

*记忆规则*：
- 只记长期有效的重要信息（姓名、生日、喜好等）
- 不记临时的事（用定时消息）
- 每个用户的记忆独立存储
- 私信时你只看到对方的记忆
- 频道里你能看到所有人的记忆
- 用户可用 /memory 查看和删除自己的记忆
- 记忆超出上限时，最早的记忆会被自动删除

*隐藏规则*：
- 设定的隐藏内容你下次能看到
- 用户要求设提醒时，自然地确认并告知设置的时间
- 当你想在某个时间给用户发消息（不一定是提醒），也可以设定时消息
- 记录特殊日期并非硬性规定，只要你认为需要记录的日期都可以是特殊日期

*时间理解规则*（���置定时消息时必须遵守）：
- 用户说的时间通常是12小时制，需要根据当前时间判断
- 如果时间有歧义，先询问确认
- 如果用户明确说了上午/下午/晚上，就不需要询问
- 定时消息格式必须包含完整日期：[[定时|YYYY-MM-DD|HH:MM|内容]]
- 使用24小时制

*回复规则*：
- 如果你觉得用户的消息不需要回复（比如只是"嗯"、"哦"、"好"、表情等），可以只回复：[不回]
- 不要滥用，正常对话还是要回复的"""

    if mode == "short":
        base += f"""

===== 短句模式 =====

你现在是短句模式，像朋友发微信一样聊天。

*回复数量规则*：
- 用户这次发了 {msg_count} 条消息
- 根据情况决定回复数量：
  • 用户发 1 条简短消息（比如"在吗"、"嗯"、"哦"）→ 你回 1 条
  • 用户发 1 条普通消息 → 你回 1-2 条
  • 用户发 1 条很长的消息或问了复杂问题 → 你可以回 2-3 条
  • 用户连续发了好几条 → 你可以相应多回几条
- 用 ||| 分隔多条消息

*风格*：
- 每条消息简短自然，像发微信
- 不要太正式，轻松一点
- 该回 1 条就 1 条，不要硬凑

*示例*：
用户：在吗
你：在~

用户：今天好累啊
你：怎么啦|||工作太多了？

用户：（发了一大段话讲了很多事情）
你：哇这也太多了吧|||一个一个说|||先说第一个..."""

    return base

def parse_hidden_commands(reply, user_id, current_channel=None):
    schedules = load_schedules()
    if user_id not in schedules:
        schedules[user_id] = {"timed": [], "daily": [], "special_dates": {}}

    has_hidden = False
    original_reply = reply
    extra_actions = []

    timed_new = re.findall(r'\[\[定时\|(\d{4}-\d{2}-\d{2})\|(\d{1,2}:\d{2})\|(.+?)\]\]', reply)
    for date_str, time_str, hint in timed_new:
        parts = time_str.split(":")
        normalized_time = f"{int(parts[0]):02d}:{parts[1]}"
        
        schedules[user_id]["timed"].append({
            "date": date_str,
            "time": normalized_time,
            "hint": hint
        })
        reply = reply.replace(f"[[定时|{date_str}|{time_str}|{hint}]]", "")
        has_hidden = True
        print(f"[Parse] 添加定时任务: {date_str} {normalized_time} - {hint[:30]}...")

    timed_old = re.findall(r'\[\[定时\|(\d{1,2}:\d{2})\|([^\]]+?)\]\]', reply)
    for time_str, hint in timed_old:
        parts = time_str.split(":")
        normalized_time = f"{int(parts[0]):02d}:{parts[1]}"
        
        schedules[user_id]["timed"].append({
            "date": get_cn_time().strftime("%Y-%m-%d"),
            "time": normalized_time,
            "hint": hint
        })
        reply = reply.replace(f"[[定时|{time_str}|{hint}]]", "")
        has_hidden = True
        print(f"[Parse] 添加定时任务(旧格式): {get_cn_time().strftime('%Y-%m-%d')} {normalized_time}")

    daily = re.findall(r'\[\[每日\|(\d{1,2}:\d{2})\|(.+?)\]\]', reply)
    for time_str, topic in daily:
        parts = time_str.split(":")
        normalized_time = f"{int(parts[0]):02d}:{parts[1]}"
        
        schedules[user_id]["daily"].append({
            "time": normalized_time,
            "topic": topic
        })
        reply = reply.replace(f"[[每日|{time_str}|{topic}]]", "")
        has_hidden = True

    mems_with_user = re.findall(r'\[\[记忆\|([A-Z0-9]+)\|(.+?)\]\]', reply)
    for mem_user_id, content in mems_with_user:
        add_memory(mem_user_id, content)
        reply = reply.replace(f"[[记忆|{mem_user_id}|{content}]]", "")
        has_hidden = True

    mems_simple = re.findall(r'\[\[记忆\|([^|]+?)\]\]', reply)
    for content in mems_simple:
        if not re.match(r'^[A-Z0-9]+$', content):
            add_memory(user_id, content)
            reply = reply.replace(f"[[记忆|{content}]]", "")
            has_hidden = True

    dates = re.findall(r'\[\[特殊日期\|(\d{2}-\d{2})\|(.+?)\]\]', reply)
    for date, desc in dates:
        schedules[user_id]["special_dates"][date] = desc
        reply = reply.replace(f"[[特殊日期|{date}|{desc}]]", "")
        has_hidden = True

    dm_messages = re.findall(r'\[\[私聊\|(.+?)\]\]', reply)
    for msg in dm_messages:
        extra_actions.append({"type": "dm", "content": msg})
        reply = reply.replace(f"[[私聊|{msg}]]", "")
        has_hidden = True

    channel_messages = re.findall(r'\[\[发到频道\|(\w+)\|(.+?)\]\]', reply)
    for ch_name, msg in channel_messages:
        extra_actions.append({"type": "to_channel", "channel_name": ch_name, "content": msg})
        reply = reply.replace(f"[[发到频道|{ch_name}|{msg}]]", "")
        has_hidden = True

    reactions = re.findall(r'\[\[反应\|(\w+)\]\]', reply)
    for emoji in reactions:
        extra_actions.append({"type": "reaction", "emoji": emoji.lower().strip()})
        reply = reply.replace(f"[[反应|{emoji}]]", "")
        has_hidden = True

    save_schedules(schedules)
    reply = re.sub(r'\n{3,}', '\n\n', reply).strip()

    return reply, has_hidden, original_reply, extra_actions

def call_ai(messages, api_name, has_image=False, max_retries=3):
    api = APIS.get(api_name, APIS[DEFAULT_API])

    if has_image and not api.get("vision", False):
        return "抱歉，当前模型不支持图片。请用 /model 切换到支持图片的模型。"

    for attempt in range(max_retries):
        try:
            print(f"调用 API: {api_name}, Model: {api['model']}, 尝试 {attempt + 1}/{max_retries}")

            resp = requests.post(
                api["url"],
                headers={
                    "Authorization": f"Bearer {api['key']}",
                    "Content-Type": "application/json"
                },
                json={"model": api["model"], "messages": messages},
                timeout=120
            )

            result = resp.json()

            if "choices" in result:
                return result["choices"][0]["message"]["content"]
            elif "error" in result:
                error_msg = result.get("error", {})
                error_str = str(error_msg).lower()
                
                if "upstream" in error_str or "do_request_failed" in error_str or "timeout" in error_str:
                    print(f"[API] 上游错误，{2 ** attempt}秒后重试...")
                    time.sleep(2 ** attempt)
                    continue
                    
                return f"API 错误: {error_msg}"
            else:
                return f"API 异常: {result}"
                
        except requests.exceptions.Timeout:
            print(f"[API] 超时，{2 ** attempt}秒后重试...")
            time.sleep(2 ** attempt)
            continue
        except Exception as e:
            print(f"异常: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return f"出错了: {str(e)}"
    
    return "API 请求失败，请稍后重试 😢"

def send_slack(channel, text):
    result = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": channel, "text": text}
    )
    return result.json().get("ts")

def update_slack(channel, ts, text):
    requests.post(
        "https://slack.com/api/chat.update",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": channel, "ts": ts, "text": text}
    )

def delete_slack(channel, ts):
    requests.post(
        "https://slack.com/api/chat.delete",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": channel, "ts": ts}
    )

def add_reaction(channel, ts, emoji):
    emoji = emoji.strip().lower().replace(':', '').replace(' ', '_')
    
    if emoji in EMOJI_ALIASES:
        original = emoji
        emoji = EMOJI_ALIASES[emoji]
        print(f"[Reaction] 别名转换: {original} -> {emoji}")
    
    if emoji not in VALID_EMOJIS:
        print(f"[Reaction] 不支持的 emoji: {emoji}，跳过")
        return
    
    try:
        result = requests.post(
            "https://slack.com/api/reactions.add",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "channel": channel,
                "timestamp": ts,
                "name": emoji
            }
        )
        resp = result.json()
        if resp.get("ok"):
            print(f"[Reaction] 添加成功: {emoji}")
        else:
            print(f"[Reaction] 添加失败: {resp.get('error')}")
    except Exception as e:
        print(f"[Reaction] 出错: {e}")

def send_multiple_slack(channel, texts):
    for text in texts:
        text = text.strip()
        if text:
            send_slack(channel, text)
            time.sleep(0.5)

def download_image(url):
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            timeout=30
        )
        if resp.status_code == 200 and len(resp.content) > 0:
            return base64.b64encode(resp.content).decode('utf-8')
    except Exception as e:
        print(f"下载失败: {e}")
    return None

def execute_extra_actions(extra_actions, user_id, current_channel, message_ts=None, current_mode="long"):
    for action in extra_actions:
        if action["type"] == "dm":
            if not is_dm_channel(current_channel):
                dm_channel = get_user_dm_channel(user_id)
                if dm_channel:
                    content = action["content"]
                    if current_mode == "short" and "|||" in content:
                        parts = content.split("|||")
                        send_multiple_slack(dm_channel, parts)
                    else:
                        send_slack(dm_channel, content)
                    print(f"[CrossChannel] 从频道发私聊消息给 {user_id}")
        
        elif action["type"] == "to_channel":
            channel_name = action["channel_name"].lower()
            target_channel = get_channel_id_by_name(channel_name)
            
            if target_channel:
                content = action["content"]
                if current_mode == "short" and "|||" in content:
                    parts = content.split("|||")
                    send_multiple_slack(target_channel, parts)
                else:
                    send_slack(target_channel, content)
                print(f"[CrossChannel] 发送消息到 #{channel_name}")
            else:
                print(f"[CrossChannel] 找不到频道: {channel_name}")
        
        elif action["type"] == "reaction" and message_ts:
            add_reaction(current_channel, message_ts, action["emoji"])

def check_pending_clear(user_id, channel):
    if user_id in pending_clear_logs:
        pending_clear_logs[user_id]["count"] -= 1
        remaining = pending_clear_logs[user_id]["count"]
        print(f"[PendingClear] 用户 {user_id} 还剩 {remaining} 条消息后清空")
        
        if remaining <= 0:
            clear_chat_logs(channel)
            log_message(channel, None, None, is_reset=True)
            del pending_clear_logs[user_id]
            print(f"[PendingClear] 用户 {user_id} 聊天记录已清空")

# ========== 处理消息 ==========

def process_message(user_id, channel, text, images=None, message_ts=None, msg_count=1):
    all_data = load_user_data()
    user = all_data.get(user_id, {
        "dm_history": [],
        "channel_history": [],
        "api": DEFAULT_API,
        "mode": "long",
        "points_used": 0
    })

    current_api = user.get("api", DEFAULT_API)
    is_dm = is_dm_channel(channel)

    can_use, remaining, msg = check_and_use_points(user_id, current_api)
    if not can_use:
        send_slack(channel, msg)
        return

    display_name = get_display_name(user_id)
    user["last_active"] = get_cn_time().timestamp()
    
    if is_dm:
        user["dm_channel"] = channel
    else:
        user["last_channel"] = channel
        # 直接在这里更新频道活动时间，不调用单独的函数
        if "channel_last_active" not in user:
            user["channel_last_active"] = {}
        user["channel_last_active"][channel] = get_cn_time().timestamp()
        print(f"[Debug] 更新频道活动时间: channel={channel}, time={user['channel_last_active'][channel]}")

    mode = user.get("mode", "long")

    log_message(channel, "user", text, username=display_name)

    system = get_system_prompt(mode, user_id, channel, msg_count)
    messages = [{"role": "system", "content": system}]
    
    history_messages = build_history_messages(user, channel, current_api)
    messages.extend(history_messages)

    has_image = False
    if images and len(images) > 0:
        has_image = True
        content = []
        if text:
            content.append({"type": "text", "text": text})
        for img_url in images:
            img_data = download_image(img_url)
            if img_data:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_data}"}
                })
        if content:
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": text or "（图片无法处理）"})
    else:
        messages.append({"role": "user", "content": text})

    typing_ts = send_slack(channel, "_Typing..._")
    reply = call_ai(messages, current_api, has_image=has_image)

    visible_reply, has_hidden, original_reply, extra_actions = parse_hidden_commands(reply, user_id, channel)

    model_name = APIS.get(current_api, {}).get("model", current_api)
    log_message(channel, "assistant", original_reply, model=model_name, hidden=has_hidden)

    current_history_key = "dm_history" if is_dm else "channel_history"
    if current_history_key not in user:
        user[current_history_key] = []
    
    # 只保存非空内容
    if text:
        user[current_history_key].append({"role": "user", "content": text})
    if original_reply:
        user[current_history_key].append({"role": "assistant", "content": original_reply})

    all_data[user_id] = user
    save_user_data(all_data)

    check_pending_clear(user_id, channel)

    execute_extra_actions(extra_actions, user_id, channel, message_ts, mode)

    if "[不回]" in visible_reply or not visible_reply.strip():
        delete_slack(channel, typing_ts)
    elif mode == "short" and "|||" in visible_reply:
        parts = visible_reply.split("|||")
        update_slack(channel, typing_ts, parts[0].strip())
        send_multiple_slack(channel, parts[1:])
    else:
        if remaining >= 0:
            visible_reply += f"\n\n_剩余积分: {remaining}_"
        update_slack(channel, typing_ts, visible_reply)

def delayed_process(user_id, channel, message_ts=None):
    time.sleep(5)

    if user_id in pending_messages and pending_messages[user_id]:
        msgs = pending_messages[user_id]
        msg_count = len(msgs)
        combined = "\n".join(msgs)
        pending_messages[user_id] = []

        all_data = load_user_data()
        user = all_data.get(user_id, {
            "dm_history": [],
            "channel_history": [],
            "api": DEFAULT_API,
            "mode": "short",
            "points_used": 0
        })

        current_api = user.get("api", DEFAULT_API)
        is_dm = is_dm_channel(channel)

        can_use, remaining, msg = check_and_use_points(user_id, current_api)
        if not can_use:
            send_slack(channel, msg)
            return

        typing_ts = send_slack(channel, "_Typing..._")

        display_name = get_display_name(user_id)
        log_message(channel, "user", combined, username=display_name)

        if is_dm:
            user["dm_channel"] = channel
        else:
            user["last_channel"] = channel
            # 直接在这里更新频道活动时间
            if "channel_last_active" not in user:
                user["channel_last_active"] = {}
            user["channel_last_active"][channel] = get_cn_time().timestamp()
            print(f"[Debug] 更新频道活动时间: channel={channel}, time={user['channel_last_active'][channel]}")

        system = get_system_prompt("short", user_id, channel, msg_count)
        messages = [{"role": "system", "content": system}]
        
        history_messages = build_history_messages(user, channel, current_api)
        messages.extend(history_messages)
        messages.append({"role": "user", "content": combined})

        reply = call_ai(messages, current_api)
        visible_reply, has_hidden, original_reply, extra_actions = parse_hidden_commands(reply, user_id, channel)

        model_name = APIS.get(current_api, {}).get("model", "未知")
        log_message(channel, "assistant", original_reply, model=model_name, hidden=has_hidden)

        current_history_key = "dm_history" if is_dm else "channel_history"
        if current_history_key not in user:
            user[current_history_key] = []
        
        # 只保存非空内容
        if combined:
            user[current_history_key].append({"role": "user", "content": combined})
        if original_reply:
            user[current_history_key].append({"role": "assistant", "content": original_reply})
        
        user["last_active"] = get_cn_time().timestamp()

        all_data[user_id] = user
        save_user_data(all_data)

        check_pending_clear(user_id, channel)

        execute_extra_actions(extra_actions, user_id, channel, message_ts, "short")

        if "[不回]" in visible_reply or not visible_reply.strip():
            delete_slack(channel, typing_ts)
        elif "|||" in visible_reply:
            parts = visible_reply.split("|||")
            update_slack(channel, typing_ts, parts[0].strip())
            send_multiple_slack(channel, parts[1:])
        else:
            if remaining >= 0:
                visible_reply += f"\n\n_剩余积分: {remaining}_"
            update_slack(channel, typing_ts, visible_reply)

# ========== Slack 事件 ==========

@app.route("/slack/events", methods=["POST"])
def events():
    data = request.json
    print(f"收到事件: {json.dumps(data, ensure_ascii=False)[:1000]}")

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event_id = data.get("event_id")
    if event_id in processed_events:
        return jsonify({"ok": True})
    processed_events.add(event_id)

    if len(processed_events) > 1000:
        processed_events.clear()

    event = data.get("event", {})
    event_type = event.get("type")
    subtype = event.get("subtype")

    if event_type in ["app_mention", "message"]:
        if event.get("bot_id"):
            return jsonify({"ok": True})
        
        if subtype and subtype not in ["file_share"]:
            return jsonify({"ok": True})

        user_id = event.get("user")
        channel = event.get("channel")
        raw_text = event.get("text", "")
        text = re.sub(r'<@\w+>', '', raw_text).strip()
        message_ts = event.get("ts")

        if text.startswith("/"):
            print(f"[Events] 忽略斜杠命令: {text}")
            return jsonify({"ok": True})

        is_dm = is_dm_channel(channel)
        is_mention = "<@" in raw_text
        in_conversation = is_in_conversation(user_id, channel)
        
        should_respond = is_dm or is_mention or in_conversation
        
        if not should_respond:
            print(f"[Events] 不响应：非私聊、未@、不在对话中")
            return jsonify({"ok": True})

        images = []
        files = event.get("files", [])
        for f in files:
            if f.get("mimetype", "").startswith("image/"):
                url = f.get("url_private")
                if url:
                    images.append(url)
                    print(f"[Events] 发现图片: {url[:50]}...")

        if not text and not images:
            return jsonify({"ok": True})

        print(f"用户 {user_id}: {text}, 图片: {len(images)}, 场景: {'私聊' if is_dm else '频道'}, @: {is_mention}, 对话中: {in_conversation}")

        all_data = load_user_data()
        user = all_data.get(user_id, {})
        mode = user.get("mode", "long")

        if mode == "short" and not images:
            if user_id not in pending_messages:
                pending_messages[user_id] = []
            pending_messages[user_id].append(text)

            if user_id in pending_timers:
                pending_timers[user_id].cancel()

            timer = threading.Timer(5.0, delayed_process, args=[user_id, channel, message_ts])
            timer.start()
            pending_timers[user_id] = timer
        else:
            threading.Thread(target=process_message, args=[user_id, channel, text, images, message_ts, 1]).start()

    return jsonify({"ok": True})

# ========== 斜杠命令 ==========

@app.route("/slack/commands", methods=["POST"])
def commands():
    cmd = request.form.get("command")
    user_id = request.form.get("user_id")
    channel = request.form.get("channel_id")
    text = request.form.get("text", "").strip().lower()

    print(f"[Debug] 命令: {cmd}, 参数: '{text}'")

    all_data = load_user_data()
    schedules = load_schedules()
    is_dm = is_dm_channel(channel)

    if cmd == "/reset":
        def do_reset():
            try:
                data = load_user_data()
                if user_id in data:
                    if is_dm:
                        data[user_id]["dm_history"] = []
                    else:
                        data[user_id]["channel_history"] = []
                    
                    data[user_id]["points_used"] = 0
                    save_user_data(data)
                
                if is_dm:
                    scheds = load_schedules()
                    if user_id in scheds:
                        scheds[user_id] = {"timed": [], "daily": [], "special_dates": {}}
                        save_schedules(scheds)
                
                scene = "私聊" if is_dm else "频道"
                print(f"[Reset] 用户 {user_id} {scene}历史已重置")
            except Exception as e:
                print(f"[Error] 重置失败: {str(e)}")
        
        threading.Thread(target=do_reset).start()
        
        pending_clear_logs[user_id] = {
            "channel": channel,
            "count": 5
        }
        
        scene = "私聊" if is_dm else "频道"
        extra_info = "、定时任务" if is_dm else ""
        
        return jsonify({
            "response_type": "in_channel",
            "text": f"✅ 已重置{scene}对话历史{extra_info}！（记忆保留）\n\n📝 聊天记录将在 *5 条消息后* 清空"
        })

    if cmd == "/memory":
        if not text:
            mem = format_memories(user_id, show_numbers=True)
            total = sum(len(m["content"]) for m in load_memories(user_id))
            if mem:
                return jsonify({"response_type": "ephemeral", "text": f"📝 你的记忆（{total}/{MEMORY_LIMIT}字）：\n{mem}"})
            else:
                return jsonify({"response_type": "ephemeral", "text": "📝 暂无记忆"})

        if text == "clear":
            def do_clear():
                try:
                    clear_memories(user_id)
                    print(f"[Memory] 用户 {user_id} 记忆已清空")
                except Exception as e:
                    print(f"[Error] 清空记忆失败: {str(e)}")
            
            threading.Thread(target=do_clear).start()
            return jsonify({"response_type": "ephemeral", "text": "✅ 记忆已清空！"})

        if text.startswith("delete "):
            try:
                index = int(text[7:].strip())
                removed = delete_memory(user_id, index)
                if removed:
                    return jsonify({"response_type": "ephemeral", "text": f"✅ 已删除第 {index} 条：{removed}"})
                else:
                    return jsonify({"response_type": "ephemeral", "text": f"❌ 没有第 {index} 条记忆"})
            except ValueError:
                return jsonify({"response_type": "ephemeral", "text": "❌ 请输入编号，如：/memory delete 1"})

        return jsonify({"response_type": "ephemeral", "text": "用法：\n/memory - 查看\n/memory clear - 清空\n/memory delete 编号 - 删除"})

    if cmd == "/model":
        if not text:
            models_info = []
            for name, info in APIS.items():
                vision = "📷" if info.get("vision") else ""
                cost = info.get("cost", 1)
                models_info.append(f"{name} ({cost}分) {vision}")

            current = all_data.get(user_id, {}).get("api", DEFAULT_API)
            points_used = all_data.get(user_id, {}).get("points_used", 0)
            remaining = POINTS_LIMIT - points_used

            if is_unlimited_user(user_id):
                points_str = "∞ 无限"
            else:
                points_str = f"{remaining}/{POINTS_LIMIT}"

            return jsonify({
                "response_type": "ephemeral", 
                "text": f"当前: {current}\n剩余积分: {points_str}\n\n可用:\n" + "\n".join(models_info)
            })

        original_text = request.form.get("text", "").strip()
        if original_text in APIS:
            if user_id not in all_data:
                all_data[user_id] = {"dm_history": [], "channel_history": [], "api": DEFAULT_API, "mode": "long", "points_used": 0}
            all_data[user_id]["api"] = original_text
            save_user_data(all_data)
            vision = "✅" if APIS[original_text].get("vision") else "❌"
            cost = APIS[original_text].get("cost", 1)
            return jsonify({"response_type": "ephemeral", "text": f"✅ {original_text}（{cost}分/次，图片{vision}）"})
        else:
            return jsonify({"response_type": "ephemeral", "text": "❌ 没有这个模型"})

    if cmd == "/mode":
        if not text:
            current = all_data.get(user_id, {}).get("mode", "long")
            return jsonify({"response_type": "ephemeral", "text": f"当前: {current}\n可用: long, short"})

        if text in ["long", "short"]:
            if user_id not in all_data:
                all_data[user_id] = {"dm_history": [], "channel_history": [], "api": DEFAULT_API, "mode": "long", "points_used": 0}
            all_data[user_id]["mode"] = text
            save_user_data(all_data)
            return jsonify({"response_type": "ephemeral", "text": f"✅ {text}"})
        else:
            return jsonify({"response_type": "ephemeral", "text": "❌ 只能 long 或 short"})

    if cmd == "/points":
        if is_unlimited_user(user_id):
            return jsonify({"response_type": "ephemeral", "text": "✨ 你是无限用户"})

        points_used = all_data.get(user_id, {}).get("points_used", 0)
        remaining = POINTS_LIMIT - points_used
        return jsonify({"response_type": "ephemeral", "text": f"剩余积分: {remaining}/{POINTS_LIMIT}"})

    return jsonify({"response_type": "ephemeral", "text": "未知命令"})

# ========== 后台定时任务线程 ==========

def run_scheduler():
    while True:
        try:
            now = get_cn_time()
            current_time = now.strftime("%H:%M")
            current_date_md = now.strftime("%m-%d")

            print(f"[Scheduler] 检查时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

            if current_time == "00:00":
                all_data = load_user_data()
                for uid in all_data:
                    all_data[uid]["points_used"] = 0
                save_user_data(all_data)
                print("[Scheduler] 积分已重置")

            all_data = load_user_data()
            schedules = load_schedules()

            for user_id, user in all_data.items():
                dm_channel = user.get("dm_channel")
                last_channel = user.get("last_channel")
                
                if not dm_channel and not last_channel:
                    dm_channel = get_user_dm_channel(user_id)
                    if dm_channel:
                        user["dm_channel"] = dm_channel
                        all_data[user_id] = user
                        print(f"[Scheduler] 为用户 {user_id} 创建了私聊频道 {dm_channel}")
                    else:
                        continue
                
                channel = dm_channel or last_channel
                if not channel:
                    continue

                user_schedules = schedules.get(user_id, {"timed": [], "daily": [], "special_dates": {}})
                current_api = user.get("api", DEFAULT_API)
                current_mode = user.get("mode", "long")

                timed = user_schedules.get("timed", [])
                new_timed = []
                
                for item in timed:
                    item_time = item.get("time", "")
                    item_date = item.get("date", "")
                    
                    if not item_time or not item_date:
                        continue
                    
                    if len(item_time.split(":")[0]) == 1:
                        item_time = "0" + item_time
                    
                    try:
                        target_datetime = datetime.strptime(f"{item_date} {item_time}", "%Y-%m-%d %H:%M")
                        target_datetime = target_datetime.replace(tzinfo=CN_TIMEZONE)
                    except Exception as e:
                        print(f"[Scheduler] 日期解析失败: {item}, 错误: {e}")
                        new_timed.append(item)
                        continue
                    
                    if now >= target_datetime:
                        hint = item.get("hint", "")
                        print(f"[Scheduler] >>> 触发定时任务: {hint[:50]}...")
                        
                        target_channel = dm_channel or channel
                        is_dm = is_dm_channel(target_channel)
                        
                        system = get_system_prompt(current_mode, user_id, target_channel, 1)
                        
                        system += f"""

===== 当前任务：定时消息 =====
你之前设定了一个定时任务：{hint}
现在时间到了。

*重要*：
- 这是你主动发消息给用户，不是在回复用户的消息
- 用户没有发任何新消息给你
- 直接说你想说的话就好

如果你觉得现在不适合发消息，回复：[不发]"""

                        messages = [{"role": "system", "content": system}]
                        
                        history_messages = build_history_messages(user, target_channel, current_api)
                        messages.extend(history_messages)

                        reply = call_ai(messages, current_api)
                        print(f"[Scheduler] AI回复: {reply[:100]}...")

                        if "[不发]" not in reply:
                            visible, has_hidden, original_reply, extra_actions = parse_hidden_commands(reply, user_id, target_channel)
                            
                            if visible.strip() and "[不回]" not in visible:
                                if current_mode == "short" and "|||" in visible:
                                    parts = visible.split("|||")
                                    send_multiple_slack(target_channel, parts)
                                else:
                                    send_slack(target_channel, visible)
                                
                                model_name = APIS.get(current_api, {}).get("model", "AI")
                                log_message(target_channel, "assistant", original_reply, model=model_name, hidden=has_hidden)
                                
                                current_history_key = "dm_history" if is_dm else "channel_history"
                                if current_history_key not in user:
                                    user[current_history_key] = []
                                if original_reply:
                                    user[current_history_key].append({"role": "assistant", "content": original_reply})
                                
                                execute_extra_actions(extra_actions, user_id, target_channel, None, current_mode)
                                
                                print(f"[Scheduler] 已发送定时消息给 {user_id}")
                    else:
                        new_timed.append(item)
                
                user_schedules["timed"] = new_timed

                for item in user_schedules.get("daily", []):
                    item_time = item.get("time", "")
                    if len(item_time.split(":")[0]) == 1:
                        item_time = "0" + item_time
                    
                    if item_time == current_time:
                        topic = item.get("topic", "")
                        print(f"[Scheduler] 触发每日任务: {topic[:30]}...")
                        
                        target_channel = dm_channel or channel
                        is_dm = is_dm_channel(target_channel)
                        
                        system = get_system_prompt(current_mode, user_id, target_channel, 1)
                        
                        system += f"""

===== 当前任务：每日消息 =====
你设定了每天这个时候发消息，主题：{topic}

*重要*：
- 这是你主动发消息给用户，不是在回复用户的消息
- 用户没有发任何新消息给你
- 直接说你想说的话就好

如果你觉得现在不适合发消息，回复：[不发]"""

                        messages = [{"role": "system", "content": system}]
                        history_messages = build_history_messages(user, target_channel, current_api)
                        messages.extend(history_messages)

                        reply = call_ai(messages, current_api)

                        if "[不发]" not in reply:
                            visible, has_hidden, original_reply, extra_actions = parse_hidden_commands(reply, user_id, target_channel)
                            
                            if visible.strip() and "[不回]" not in visible:
                                if current_mode == "short" and "|||" in visible:
                                    parts = visible.split("|||")
                                    send_multiple_slack(target_channel, parts)
                                else:
                                    send_slack(target_channel, visible)
                                
                                model_name = APIS.get(current_api, {}).get("model", "AI")
                                log_message(target_channel, "assistant", original_reply, model=model_name, hidden=has_hidden)
                                
                                current_history_key = "dm_history" if is_dm else "channel_history"
                                if current_history_key not in user:
                                    user[current_history_key] = []
                                if original_reply:
                                    user[current_history_key].append({"role": "assistant", "content": original_reply})
                                
                                execute_extra_actions(extra_actions, user_id, target_channel, None, current_mode)
                                
                                print(f"[Scheduler] 已发送每日消息给 {user_id}")

                if current_time == "00:00":
                    special_dates = user_schedules.get("special_dates", {})
                    if current_date_md in special_dates:
                        desc = special_dates[current_date_md]
                        print(f"[Scheduler] 触发特殊日期: {desc[:30]}...")
                        
                        target_channel = dm_channel or channel
                        is_dm = is_dm_channel(target_channel)
                        
                        system = get_system_prompt(current_mode, user_id, target_channel, 1)
                        
                        system += f"""

===== 当前任务：特殊日期 =====
今天是用户的特殊日子：{desc}

*重要*：
- 这是你主动发消息祝福用户
- 用户没有发任何新消息给你
- 发一条温馨的消息就好

如果你觉得不合适，回复：[不发]"""

                        messages = [{"role": "system", "content": system}]
                        history_messages = build_history_messages(user, target_channel, current_api)
                        messages.extend(history_messages)

                        reply = call_ai(messages, current_api)

                        if "[不发]" not in reply:
                            visible, has_hidden, original_reply, extra_actions = parse_hidden_commands(reply, user_id, target_channel)
                            
                            if visible.strip() and "[不回]" not in visible:
                                if current_mode == "short" and "|||" in visible:
                                    parts = visible.split("|||")
                                    send_multiple_slack(target_channel, parts)
                                else:
                                    send_slack(target_channel, visible)
                                
                                model_name = APIS.get(current_api, {}).get("model", "AI")
                                log_message(target_channel, "assistant", original_reply, model=model_name, hidden=has_hidden)
                                
                                current_history_key = "dm_history" if is_dm else "channel_history"
                                if current_history_key not in user:
                                    user[current_history_key] = []
                                if original_reply:
                                    user[current_history_key].append({"role": "assistant", "content": original_reply})
                                
                                execute_extra_actions(extra_actions, user_id, target_channel, None, current_mode)
                                
                                print(f"[Scheduler] 已发送特殊日期消息给 {user_id}")

                schedules[user_id] = user_schedules

            save_schedules(schedules)
            save_user_data(all_data)

        except Exception as e:
            print(f"[Scheduler] 出错: {str(e)}")
            import traceback
            traceback.print_exc()

        time.sleep(60)

@app.route("/cron", methods=["GET", "POST"])
def cron_job():
    return jsonify({"ok": True, "message": "Using background thread scheduler"})

@app.route("/")
def home():
    return "Bot is running! 🤖"

scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()
print("[Startup] 后台定时任务线程已启动")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

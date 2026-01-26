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
    "opus": 190000
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
    }
}

DEFAULT_API = "第三方sonnet"
UNLIMITED_USERS = ["sakuragochyan"]
POINTS_LIMIT = 20
MEMORY_LIMIT = 2000

CN_TIMEZONE = timezone(timedelta(hours=8))

processed_events = set()
pending_messages = {}
pending_timers = {}
pending_clear_logs = {}

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

# ========== 历史记录管理 ==========

def estimate_tokens(text):
    if not text:
        return 0
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', str(text)))
    other_chars = len(str(text)) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)

def trim_history_for_api(history, api_name, max_ratio=1.0):
    max_tokens = int(API_TOKEN_LIMITS.get(api_name, 100000) * max_ratio)
    
    total_tokens = sum(estimate_tokens(m.get("content", "")) for m in history)
    
    while total_tokens > max_tokens and len(history) > 2:
        removed = history.pop(0)
        total_tokens -= estimate_tokens(removed.get("content", ""))
    
    return history

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
    except:
        pass
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
    
    base = f"""你是一个友好的AI助手。当前时间（中国时间）: {get_time_str()}
当前场景：{current_scene}
{memories_text}

Slack 格式规则：
- 粗体：*文字*
- 斜体：_文字_
- 删除线：~文字~
- 代码：`代码` 或 ```代码块```
- 列表：• 或 1. 2. 3.
- 引用：> 开头

禁止：# 标题、LaTeX、Markdown 表格

===== 场景意识（重要！）=====
- 你要清楚知道用户是在私聊还是在频道跟你说话
- 私聊记录和频道记录会分开显示给你，注意区分
- 如果用户在频道里回复了你在私聊问的问题，你应该觉得奇怪并指出
- 私聊的内容不要在频道里随便提起（除非用户主动说）
- 有些话题更适合私聊，你可以建议"这个我们私下聊？"
- 频道是公开的，说话要注意

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
   [[发送到频道|内容]] - 在私聊时发消息到频道

6. *表情反应*：
   [[反应|emoji名称]] - 给用户的消息加表情
   例如：[[反应|heart]] [[反应|eyes]] [[反应|thumbsup]]
   常用：heart, eyes, thumbsup, joy, thinking_face, fire, sparkles, wave

*记忆规则*：
- 只记长期有效的重要信息（姓名、生日、喜好等）
- 不记临时的事（用定时消息）
- 每个用户的记忆独立存储
- 私信时你只看到对方的记忆
- 频道里你能看到所有人的记忆
- 用户可用 /memory 查看和删除自己的记忆

*隐藏规则*：
- 设定的隐藏内容你下次能看到
- 用户要求设提醒时，自然地确认并告知设置的时间
- 当你想在某个时间给用户发消息（不一定是提醒），也可以设定时消息
- 记录特殊日期并非硬性规定，只要你认为需要记录的日期都可以是特殊日期

*时间理解规则*（设置定时消息时必须遵守）：
- 用户说的时间通常是12小时制，需要根据当前时间判断
- 如果时间有歧义，先询问确认
- 如果用户明确说了上午/下午/晚上，就不需要询问
- 定时消息格式必须包含完整日期：[[定时|YYYY-MM-DD|HH:MM|内容]]
- 使用24小时制

*回复规则*：
- 如果你觉得用户的消息不需要回复（比如只是"嗯"、"哦"、"好"、表情等），可以只加个表情反应，或回复：[不回]
- 不要滥用，正常对话还是要回复的"""

    if mode == "short":
        base += f"""

===== 短句模式（重要！必须遵守）=====

你现在是短句模式，像真人聊天一样：

*回复数量规则*：
- 用户发了 {msg_count} 条消息
- 你应该回复 1-{min(msg_count + 1, 3)} 条左右
- 大部分情况 1-2 条就够了
- 只有用户发很多或问了复杂问题才回多条
- 用 ||| 分隔多条消息

*风格*：
- 每条消息简短（1-2句话）
- 像朋友聊天，不要太正式
- 不要每次都回3条以上，很奇怪

示例：
用户：在吗
你：在呀

用户：今天天气怎么样
你：还不错哦|||挺适合出门的

用户发了很长的问题
你：好的|||我来想想|||（回答内容）"""

    return base

def parse_hidden_commands(reply, user_id, current_channel=None):
    schedules = load_schedules()
    if user_id not in schedules:
        schedules[user_id] = {"timed": [], "daily": [], "special_dates": {}}

    has_hidden = False
    original_reply = reply
    extra_actions = []

    # 新格式：[[定时|YYYY-MM-DD|HH:MM|内容]]
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

    # 兼容旧格式
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

    # 每日消息
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

    # 记忆
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

    # 特殊日期
    dates = re.findall(r'\[\[特殊日期\|(\d{2}-\d{2})\|(.+?)\]\]', reply)
    for date, desc in dates:
        schedules[user_id]["special_dates"][date] = desc
        reply = reply.replace(f"[[特殊日期|{date}|{desc}]]", "")
        has_hidden = True

    # 跨场景：私聊
    dm_messages = re.findall(r'\[\[私聊\|(.+?)\]\]', reply)
    for msg in dm_messages:
        extra_actions.append({"type": "dm", "content": msg})
        reply = reply.replace(f"[[私聊|{msg}]]", "")
        has_hidden = True

    # 跨场景：发送到频道
    channel_messages = re.findall(r'\[\[发送到频道\|(.+?)\]\]', reply)
    for msg in channel_messages:
        extra_actions.append({"type": "channel", "content": msg})
        reply = reply.replace(f"[[发送到频道|{msg}]]", "")
        has_hidden = True

    # 表情反应
    reactions = re.findall(r'\[\[反应\|(.+?)\]\]', reply)
    for emoji in reactions:
        extra_actions.append({"type": "reaction", "emoji": emoji})
        reply = reply.replace(f"[[反应|{emoji}]]", "")
        has_hidden = True

    save_schedules(schedules)
    reply = re.sub(r'\n{3,}', '\n\n', reply).strip()

    return reply, has_hidden, original_reply, extra_actions

def call_ai(messages, api_name, has_image=False):
    api = APIS.get(api_name, APIS[DEFAULT_API])

    if has_image and not api.get("vision", False):
        return "抱歉，当前模型不支持图片。请用 /model 切换到 sonnet 或 opus。"

    try:
        print(f"调用 API: {api_name}, Model: {api['model']}")

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
            return f"API 错误: {result['error']}"
        else:
            return f"API 异常: {result}"
    except Exception as e:
        print(f"异常: {str(e)}")
        return f"出错了: {str(e)}"

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
    """给消息添加表情反应"""
    emoji = emoji.strip().strip(':').lower()
    
    # emoji 映射
    emoji_map = {
        '👀': 'eyes',
        '❤️': 'heart',
        '😀': 'grinning',
        '😂': 'joy',
        '🤔': 'thinking_face',
        '👍': 'thumbsup',
        '👎': 'thumbsdown',
        '🎉': 'tada',
        '🔥': 'fire',
        '💯': '100',
        '😊': 'blush',
        '😢': 'cry',
        '🙏': 'pray',
        '✨': 'sparkles',
        '💪': 'muscle',
        '🤗': 'hugs',
        '😴': 'sleeping',
        '😍': 'heart_eyes',
        '👋': 'wave',
        '☀️': 'sunny',
        '⭐': 'star',
        '💕': 'two_hearts',
        '😭': 'sob',
        '✅': 'white_check_mark',
        '❌': 'x',
    }
    
    if emoji in emoji_map:
        emoji = emoji_map[emoji]
    
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
    """执行额外操作"""
    all_data = load_user_data()
    user = all_data.get(user_id, {})
    
    for action in extra_actions:
        if action["type"] == "dm":
            dm_channel = get_user_dm_channel(user_id)
            if dm_channel and dm_channel != current_channel:
                content = action["content"]
                if current_mode == "short" and "|||" in content:
                    parts = content.split("|||")
                    send_multiple_slack(dm_channel, parts)
                else:
                    send_slack(dm_channel, content)
                print(f"[CrossChannel] 发送私聊消息给 {user_id}")
        
        elif action["type"] == "channel":
            target_channel = user.get("last_channel")
            if target_channel and is_dm_channel(current_channel):
                content = action["content"]
                if current_mode == "short" and "|||" in content:
                    parts = content.split("|||")
                    send_multiple_slack(target_channel, parts)
                else:
                    send_slack(target_channel, content)
                print(f"[CrossChannel] 发送频道消息到 {target_channel}")
        
        elif action["type"] == "reaction" and message_ts:
            add_reaction(current_channel, message_ts, action["emoji"])

# ========== 检查并清空聊天记录 ==========

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
    user["dm_channel"] = channel if is_dm else user.get("dm_channel")
    user["last_channel"] = channel if not is_dm else user.get("last_channel")

    mode = user.get("mode", "long")

    log_message(channel, "user", text, username=display_name)

    system = get_system_prompt(mode, user_id, channel, msg_count)
    messages = [{"role": "system", "content": system}]
    
    current_history_key = "dm_history" if is_dm else "channel_history"
    other_history_key = "channel_history" if is_dm else "dm_history"
    
    current_history = user.get(current_history_key, []).copy()
    other_history = user.get(other_history_key, []).copy()
    
    # 添加其他场景历史作为参考
    if other_history:
        other_scene = "频道" if is_dm else "私聊"
        other_history_trimmed = trim_history_for_api(other_history.copy(), current_api, 0.3)
        if other_history_trimmed:
            context_text = f"===== 以下是{other_scene}的近期记录（参考用）=====\n"
            for m in other_history_trimmed[-10:]:
                role_name = "用户" if m["role"] == "user" else "AI"
                context_text += f"{role_name}: {m['content']}\n"
            messages.append({"role": "system", "content": context_text})
    
    # 添加当前场景历史
    current_history_trimmed = trim_history_for_api(current_history.copy(), current_api, 0.6)
    messages.extend(current_history_trimmed)

    # 添加当前消息
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

    # 保存历史
    if current_history_key not in user:
        user[current_history_key] = []
    user[current_history_key].append({"role": "user", "content": text})
    user[current_history_key].append({"role": "assistant", "content": original_reply})

    all_data[user_id] = user
    save_user_data(all_data)

    check_pending_clear(user_id, channel)

    # 执行额外操作
    execute_extra_actions(extra_actions, user_id, channel, message_ts, mode)

    # 处理回复
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

        system = get_system_prompt("short", user_id, channel, msg_count)
        messages = [{"role": "system", "content": system}]
        
        current_history_key = "dm_history" if is_dm else "channel_history"
        other_history_key = "channel_history" if is_dm else "dm_history"
        
        current_history = user.get(current_history_key, []).copy()
        other_history = user.get(other_history_key, []).copy()
        
        if other_history:
            other_scene = "频道" if is_dm else "私聊"
            other_history_trimmed = trim_history_for_api(other_history.copy(), current_api, 0.3)
            if other_history_trimmed:
                context_text = f"===== 以下是{other_scene}的近期记录（参考用）=====\n"
                for m in other_history_trimmed[-10:]:
                    role_name = "用户" if m["role"] == "user" else "AI"
                    context_text += f"{role_name}: {m['content']}\n"
                messages.append({"role": "system", "content": context_text})
        
        current_history_trimmed = trim_history_for_api(current_history.copy(), current_api, 0.6)
        messages.extend(current_history_trimmed)
        messages.append({"role": "user", "content": combined})

        reply = call_ai(messages, current_api)
        visible_reply, has_hidden, original_reply, extra_actions = parse_hidden_commands(reply, user_id, channel)

        model_name = APIS.get(current_api, {}).get("model", "未知")
        log_message(channel, "assistant", original_reply, model=model_name, hidden=has_hidden)

        if current_history_key not in user:
            user[current_history_key] = []
        user[current_history_key].append({"role": "user", "content": combined})
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

    if event.get("type") in ["app_mention", "message"]:
        if event.get("bot_id"):
            return jsonify({"ok": True})
        if event.get("subtype"):
            return jsonify({"ok": True})

        user_id = event.get("user")
        channel = event.get("channel")
        raw_text = event.get("text", "")
        text = re.sub(r'<@\w+>', '', raw_text).strip()
        message_ts = event.get("ts")

        if text.startswith("/"):
            print(f"[Events] 忽略斜杠命令: {text}")
            return jsonify({"ok": True})

        images = []
        files = event.get("files", [])
        for f in files:
            if f.get("mimetype", "").startswith("image/"):
                url = f.get("url_private")
                if url:
                    images.append(url)

        if not text and not images:
            return jsonify({"ok": True})

        print(f"用户 {user_id}: {text}, 图片: {len(images)}")

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
            hour = now.hour

            print(f"[Scheduler] 检查时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

            # 午夜重置积分
            if current_time == "00:00":
                all_data = load_user_data()
                for uid in all_data:
                    all_data[uid]["points_used"] = 0
                save_user_data(all_data)
                print("[Scheduler] 积分已重置")

            all_data = load_user_data()
            schedules = load_schedules()
            schedules_changed = False

            for user_id, user in all_data.items():
                dm_channel = user.get("dm_channel")
                last_channel = user.get("last_channel")
                channel = dm_channel or last_channel
                
                if not channel:
                    print(f"[Scheduler] 用户 {user_id} 没有频道记录，跳过")
                    continue

                user_schedules = schedules.get(user_id, {"timed": [], "daily": [], "special_dates": {}})
                current_api = user.get("api", DEFAULT_API)
                current_mode = user.get("mode", "long")

                # ===== 处理定时任务 =====
                timed = user_schedules.get("timed", [])
                new_timed = []
                
                print(f"[Scheduler] 用户 {user_id} 有 {len(timed)} 个定时任务")
                
                for item in timed:
                    item_time = item.get("time", "")
                    item_date = item.get("date", "")
                    
                    if not item_time or not item_date:
                        print(f"[Scheduler] 任务缺少时间或日期，跳过: {item}")
                        continue
                    
                    # 标准化时间
                    if len(item_time.split(":")[0]) == 1:
                        item_time = "0" + item_time
                    
                    try:
                        target_datetime = datetime.strptime(f"{item_date} {item_time}", "%Y-%m-%d %H:%M")
                        target_datetime = target_datetime.replace(tzinfo=CN_TIMEZONE)
                        
                        print(f"[Scheduler] 检查任务: 目标={item_date} {item_time}, 当前={now.strftime('%Y-%m-%d %H:%M')}, 触发={now >= target_datetime}")
                        
                    except Exception as e:
                        print(f"[Scheduler] 日期解析失败: {item}, 错误: {e}")
                        new_timed.append(item)
                        continue
                    
                    if now >= target_datetime:
                        hint = item.get("hint", "")
                        print(f"[Scheduler] >>> 触发定时任务: {hint[:50]}...")
                        
                        target_channel = dm_channel or channel
                        is_dm = is_dm_channel(target_channel)
                        
                        system = get_system_prompt(current_mode, user_id, target_channel)
                        system += f"""

===== 定时提醒任务 =====
你之前设定了一个提醒：{hint}
现在时间到了。请直接发消息，不需要额外说明这是定时消息。

如果觉得现在不合适，回复：[不发]"""

                        messages = [{"role": "system", "content": system}]
                        
                        current_history_key = "dm_history" if is_dm else "channel_history"
                        history = user.get(current_history_key, []).copy()
                        history = trim_history_for_api(history, current_api, 0.6)
                        messages.extend(history)

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
                                log_message(target_channel, "assistant", f"[定时] {original_reply}", model=model_name, hidden=has_hidden)
                                
                                if current_history_key not in user:
                                    user[current_history_key] = []
                                user[current_history_key].append({"role": "assistant", "content": original_reply})
                                
                                execute_extra_actions(extra_actions, user_id, target_channel, None, current_mode)
                                
                                print(f"[Scheduler] 已发送定时消息给 {user_id}")
                            else:
                                print(f"[Scheduler] 可见回复为空或不回")
                        else:
                            print(f"[Scheduler] AI选择不发送")
                        
                        schedules_changed = True
                    else:
                        new_timed.append(item)
                
                user_schedules["timed"] = new_timed

                # ===== 处理每日任务 =====
                for item in user_schedules.get("daily", []):
                    item_time = item.get("time", "")
                    if len(item_time.split(":")[0]) == 1:
                        item_time = "0" + item_time
                    
                    if item_time == current_time:
                        topic = item.get("topic", "")
                        print(f"[Scheduler] 触发每日任务: {topic[:30]}...")
                        
                        target_channel = dm_channel or channel
                        is_dm = is_dm_channel(target_channel)
                        
                        system = get_system_prompt(current_mode, user_id, target_channel)
                        system += f"""

===== 每日消息任务 =====
你设定了每天这个时候发消息，主题：{topic}
请直接发消息，不需要说明这是每日消息。

如果觉得现在不合适，回复：[不发]"""

                        messages = [{"role": "system", "content": system}]
                        
                        current_history_key = "dm_history" if is_dm else "channel_history"
                        history = user.get(current_history_key, []).copy()
                        history = trim_history_for_api(history, current_api, 0.6)
                        messages.extend(history)

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
                                log_message(target_channel, "assistant", f"[每日] {original_reply}", model=model_name, hidden=has_hidden)
                                
                                if current_history_key not in user:
                                    user[current_history_key] = []
                                user[current_history_key].append({"role": "assistant", "content": original_reply})
                                
                                execute_extra_actions(extra_actions, user_id, target_channel, None, current_mode)
                                
                                print(f"[Scheduler] 已发送每日消息给 {user_id}")

                # ===== 处理特殊日期 =====
                if current_time == "00:00":
                    special_dates = user_schedules.get("special_dates", {})
                    if current_date_md in special_dates:
                        desc = special_dates[current_date_md]
                        print(f"[Scheduler] 触发特殊日期: {desc[:30]}...")
                        
                        target_channel = dm_channel or channel
                        is_dm = is_dm_channel(target_channel)
                        
                        system = get_system_prompt(current_mode, user_id, target_channel)
                        system += f"""

===== 特殊日期任务 =====
今天是用户的特殊日子：{desc}
请发一条温馨的消息。

如果觉得不合适，回复：[不发]"""

                        messages = [{"role": "system", "content": system}]
                        
                        current_history_key = "dm_history" if is_dm else "channel_history"
                        history = user.get(current_history_key, []).copy()
                        history = trim_history_for_api(history, current_api, 0.6)
                        messages.extend(history)

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
                                log_message(target_channel, "assistant", f"[特殊] {original_reply}", model=model_name, hidden=has_hidden)
                                
                                if current_history_key not in user:
                                    user[current_history_key] = []
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

# ========== 备用 Cron 端点 ==========

@app.route("/cron", methods=["GET", "POST"])
def cron_job():
    return jsonify({"ok": True, "message": "Using background thread scheduler"})

# ========== 首页 ==========

@app.route("/")
def home():
    return "Bot is running! 🤖"

# ========== 启动后台线程 ==========

scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()
print("[Startup] 后台定时任务线程已启动")

# ========== 启动 ==========

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

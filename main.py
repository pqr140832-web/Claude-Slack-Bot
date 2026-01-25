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

def clear_chat_logs(channel):
    logs = load_chat_logs()
    logs[channel] = []
    save_chat_logs(logs)

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

# ========== 历史记录管理 ==========

def estimate_tokens(text):
    if not text:
        return 0
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', str(text)))
    other_chars = len(str(text)) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)

def trim_history_for_api(history, api_name):
    max_tokens = API_TOKEN_LIMITS.get(api_name, 100000)
    
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

def get_system_prompt(mode="long", user_id=None, channel=None):
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

    base = f"""你是一个友好的AI助手。当前时间（中国时间）: {get_time_str()}
{memories_text}

Slack 格式规则：
- 粗体：*文字*
- 斜体：_文字_
- 删除线：~文字~
- 代码：`代码` 或 ```代码块```
- 列表：• 或 1. 2. 3.
- 引用：> 开头

禁止：# 标题、LaTeX、Markdown 表格

===== 你的特殊能力 =====

用 [[隐藏]] 包裹的内容不会发给用户，但你能看到：

1. *定时消息*：
   [[定时|HH:MM|提示内容]]

2. *每日消息*：
   [[每日|HH:MM|主题]]

3. *长期记忆*（每人限2000字）：
   [[记忆|用户ID|内容]] 或 [[记忆|内容]]（默认当前用户）

4. *特殊日期*（0:00触发）：
   [[特殊日期|MM-DD|描述]]

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
- 当你认为某一时间会想念用户或只是想在某一时间给用户主动发消息，你也可以设置定时消息、每日消息或特殊日期
- 记录特殊日期并非硬性规定，只要你认为需要记录的日期都可以是特殊日期。特殊日期最好也一并记入长期记忆"""

    if mode == "short":
        base += """

===== 短句模式（重要！必须遵守）=====

你现在是短句模式，必须：
1. 每条消息都要简短（1-2句话）
2. 用 ||| 分隔多条消息
3. 像真人聊天一样，一条一条发

示例：
用户：今天天气怎么样？
你：今天天气不错哦|||阳光明媚的|||适合出去走走~

用户：帮我写个代码
你：好的|||你要写什么呢？|||告诉我需求~

禁止：一大段话不分隔
必须：用 ||| 分成多条短消息"""

    return base

def parse_hidden_commands(reply, user_id):
    schedules = load_schedules()
    if user_id not in schedules:
        schedules[user_id] = {"timed": [], "daily": [], "special_dates": {}}

    has_hidden = False
    original_reply = reply

    timed = re.findall(r'\[\[定时\|(\d{1,2}:\d{2})\|(.+?)\]\]', reply)
    for time_str, hint in timed:
        schedules[user_id]["timed"].append({
            "time": time_str,
            "hint": hint,
            "date": get_cn_time().strftime("%Y-%m-%d")
        })
        reply = reply.replace(f"[[定时|{time_str}|{hint}]]", "")
        has_hidden = True

    daily = re.findall(r'\[\[每日\|(\d{1,2}:\d{2})\|(.+?)\]\]', reply)
    for time_str, topic in daily:
        schedules[user_id]["daily"].append({
            "time": time_str,
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

    save_schedules(schedules)
    reply = re.sub(r'\n{3,}', '\n\n', reply).strip()

    return reply, has_hidden, original_reply

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

# ========== 处理消息 ==========

def process_message(user_id, channel, text, images=None):
    all_data = load_user_data()
    user = all_data.get(user_id, {
        "history": [],
        "api": DEFAULT_API,
        "mode": "long",
        "points_used": 0
    })

    current_api = user.get("api", DEFAULT_API)

    can_use, remaining, msg = check_and_use_points(user_id, current_api)
    if not can_use:
        send_slack(channel, msg)
        return

    display_name = get_display_name(user_id)
    user["last_active"] = get_cn_time().timestamp()
    user["channel"] = channel

    mode = user.get("mode", "long")

    log_message(channel, "user", text, username=display_name)

    system = get_system_prompt(mode, user_id, channel)
    messages = [{"role": "system", "content": system}]
    
    history = trim_history_for_api(user.get("history", []).copy(), current_api)
    messages.extend(history)

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

    visible_reply, has_hidden, original_reply = parse_hidden_commands(reply, user_id)

    model_name = APIS.get(current_api, {}).get("model", current_api)
    log_message(channel, "assistant", original_reply, model=model_name, hidden=has_hidden)

    user["history"].append({"role": "user", "content": text})
    user["history"].append({"role": "assistant", "content": original_reply})

    all_data[user_id] = user
    save_user_data(all_data)

    if not visible_reply.strip():
        delete_slack(channel, typing_ts)
    elif mode == "short" and "|||" in visible_reply:
        parts = visible_reply.split("|||")
        update_slack(channel, typing_ts, parts[0].strip())
        send_multiple_slack(channel, parts[1:])
    else:
        if remaining >= 0:
            visible_reply += f"\n\n_剩余积分: {remaining}_"
        update_slack(channel, typing_ts, visible_reply)

def delayed_process(user_id, channel):
    time.sleep(5)

    if user_id in pending_messages and pending_messages[user_id]:
        combined = "\n".join(pending_messages[user_id])
        pending_messages[user_id] = []

        all_data = load_user_data()
        user = all_data.get(user_id, {
            "history": [],
            "api": DEFAULT_API,
            "mode": "short",
            "points_used": 0
        })

        current_api = user.get("api", DEFAULT_API)

        can_use, remaining, msg = check_and_use_points(user_id, current_api)
        if not can_use:
            send_slack(channel, msg)
            return

        typing_ts = send_slack(channel, "_Typing..._")

        display_name = get_display_name(user_id)
        log_message(channel, "user", combined, username=display_name)

        system = get_system_prompt("short", user_id, channel)
        messages = [{"role": "system", "content": system}]
        
        history = trim_history_for_api(user.get("history", []).copy(), current_api)
        messages.extend(history)
        messages.append({"role": "user", "content": combined})

        reply = call_ai(messages, current_api)
        visible_reply, has_hidden, original_reply = parse_hidden_commands(reply, user_id)

        model_name = APIS.get(current_api, {}).get("model", "未知")
        log_message(channel, "assistant", original_reply, model=model_name, hidden=has_hidden)

        user["history"].append({"role": "user", "content": combined})
        user["history"].append({"role": "assistant", "content": original_reply})
        user["last_active"] = get_cn_time().timestamp()

        all_data[user_id] = user
        save_user_data(all_data)

        if not visible_reply.strip():
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
        text = re.sub(r'<@\w+>', '', event.get("text", "")).strip()

        images = []
        files = event.get("files", [])
        for f in files:
            if f.get("mimetype", "").startswith("image/"):
                url = f.get("url_private")
                if url:
                    images.append(url)

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

            timer = threading.Timer(5.0, delayed_process, args=[user_id, channel])
            timer.start()
            pending_timers[user_id] = timer
        else:
            threading.Thread(target=process_message, args=[user_id, channel, text, images]).start()

    return jsonify({"ok": True})

# ========== 斜杠命令 ==========

@app.route("/slack/commands", methods=["POST"])
def commands():
    cmd = request.form.get("command")
    user_id = request.form.get("user_id")
    channel = request.form.get("channel_id")
    text = request.form.get("text", "").strip().lower()

    all_data = load_user_data()
    schedules = load_schedules()

    if cmd == "/reset":
        if text == "yes":
            # 清空 user_data
            if user_id in all_data:
                # 保留 channel 信息以便定时任务
                saved_channel = all_data[user_id].get("channel")
                all_data[user_id] = {
                    "history": [],
                    "api": DEFAULT_API,
                    "mode": "long",
                    "points_used": 0,
                    "channel": saved_channel
                }
                save_user_data(all_data)
            
            # 清空 schedules
            if user_id in schedules:
                schedules[user_id] = {"timed": [], "daily": [], "special_dates": {}}
                save_schedules(schedules)
            
            # 清空 chat_logs
            clear_chat_logs(channel)
            log_message(channel, None, None, is_reset=True)
            
            return jsonify({
                "response_type": "in_channel",
                "text": "✅ 已重置！对话历史、用户数据、聊天记录、定时任务已清空（记忆保留）"
            })
        
        elif text == "no":
            return jsonify({
                "response_type": "ephemeral",
                "text": "❌ 已取消重置"
            })
        
        else:
            return jsonify({
                "response_type": "ephemeral",
                "text": "⚠️ *确定要重置吗？*\n\n将清空：对话历史、用户数据、聊天记录、定时任务\n保留：记忆\n\n📝 现在可以去 JSONBin 备份\n\n确认请输入：`/reset yes`\n取消请输入：`/reset no`"
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
            return jsonify({
                "response_type": "ephemeral",
                "text": "⚠️ *确定要清空所有记忆吗？*\n\n📝 现在可以去 JSONBin 备份\n\n确认请输入：`/memory clear yes`\n取消请输入：`/memory clear no`"
            })
        
        if text == "clear yes":
            clear_memories(user_id)
            return jsonify({"response_type": "ephemeral", "text": "✅ 记忆已清空！"})
        
        if text == "clear no":
            return jsonify({"response_type": "ephemeral", "text": "❌ 已取消"})

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

        if text in APIS:
            if user_id not in all_data:
                all_data[user_id] = {"history": [], "api": DEFAULT_API, "mode": "long", "points_used": 0}
            all_data[user_id]["api"] = text
            save_user_data(all_data)
            vision = "✅" if APIS[text].get("vision") else "❌"
            cost = APIS[text].get("cost", 1)
            return jsonify({"response_type": "ephemeral", "text": f"✅ {text}（{cost}分/次，图片{vision}）"})
        else:
            return jsonify({"response_type": "ephemeral", "text": "❌ 没有这个模型"})

    if cmd == "/mode":
        if not text:
            current = all_data.get(user_id, {}).get("mode", "long")
            return jsonify({"response_type": "ephemeral", "text": f"当前: {current}\n可用: long, short"})

        if text in ["long", "short"]:
            if user_id not in all_data:
                all_data[user_id] = {"history": [], "api": DEFAULT_API, "mode": "long", "points_used": 0}
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
            current_date = now.strftime("%m-%d")
            hour = now.hour

            print(f"[Scheduler] 检查时间: {current_time}")

            # 每天 0:00 重置积分
            if current_time == "00:00":
                all_data = load_user_data()
                for uid in all_data:
                    all_data[uid]["points_used"] = 0
                save_user_data(all_data)
                print("[Scheduler] 积分已重置")

            all_data = load_user_data()
            schedules = load_schedules()

            for user_id, user in all_data.items():
                channel = user.get("channel")
                if not channel:
                    continue

                user_schedules = schedules.get(user_id, {"timed": [], "daily": [], "special_dates": {}})
                current_api = user.get("api", DEFAULT_API)
                memories = format_memories(user_id, show_numbers=False)

                # 定时消息
                timed = user_schedules.get("timed", [])
                new_timed = []
                for item in timed:
                    if item["time"] == current_time and item.get("date") == now.strftime("%Y-%m-%d"):
                        hint = item.get("hint", "")
                        system = f"""当前时间: {get_time_str()}

记忆：{memories if memories else "无"}

你之前设定了一个提醒：{hint}
现在时间到了。

你可以：
- 直接发消息给用户
- 如果觉得现在不合适，回复：[不发]"""

                        messages = [{"role": "system", "content": system}]
                        history = trim_history_for_api(user.get("history", [])[-10:], current_api)
                        messages.extend(history)

                        reply = call_ai(messages, current_api)

                        if "[不发]" not in reply:
                            visible, _, _ = parse_hidden_commands(reply, user_id)
                            if visible.strip():
                                send_slack(channel, visible)
                                log_message(channel, "assistant", f"[定时] {visible}", model="AI")
                                print(f"[Scheduler] 发送定时消息给 {user_id}")
                    else:
                        new_timed.append(item)
                user_schedules["timed"] = new_timed

                # 每日消息
                for item in user_schedules.get("daily", []):
                    if item["time"] == current_time:
                        topic = item.get("topic", "")
                        system = f"""当前时间: {get_time_str()}

记忆：{memories if memories else "无"}

你设定了每天这个时候发消息，主题：{topic}

你可以：
- 直接发消息给用户
- 如果觉得现在不合适，回复：[不发]"""

                        messages = [{"role": "system", "content": system}]
                        history = trim_history_for_api(user.get("history", [])[-10:], current_api)
                        messages.extend(history)

                        reply = call_ai(messages, current_api)

                        if "[不发]" not in reply:
                            visible, _, _ = parse_hidden_commands(reply, user_id)
                            if visible.strip():
                                send_slack(channel, visible)
                                log_message(channel, "assistant", f"[每日] {visible}", model="AI")
                                print(f"[Scheduler] 发送每日消息给 {user_id}")

                # 特殊日期 (0:00)
                if current_time == "00:00":
                    special_dates = user_schedules.get("special_dates", {})
                    if current_date in special_dates:
                        desc = special_dates[current_date]
                        system = f"""当前时间: {get_time_str()}

记忆：{memories if memories else "无"}

今天是用户的特殊日子：{desc}

你可以：
- 发一条温馨的消息
- 如果觉得不合适，回复：[不发]"""

                        messages = [{"role": "system", "content": system}]

                        reply = call_ai(messages, current_api)

                        if "[不发]" not in reply:
                            visible, _, _ = parse_hidden_commands(reply, user_id)
                            if visible.strip():
                                send_slack(channel, visible)
                                log_message(channel, "assistant", f"[特殊] {visible}", model="AI")
                                print(f"[Scheduler] 发送特殊日期消息给 {user_id}")

                # 不活跃检查（4-6小时随机主动发消息）
                if now.minute in [0, 30] and 7 <= hour < 23:
                    last_active = user.get("last_active", 0)
                    inactive_hours = (now.timestamp() - last_active) / 3600
                    trigger_hours = random.uniform(4, 6)

                    if inactive_hours >= trigger_hours:
                        system = f"""当前时间: {get_time_str()}

记忆：{memories if memories else "无"}

用户已经 {inactive_hours:.1f} 小时没说话了。

你可以：
- 主动发消息给用户
- 如果不想打扰，回复：[不发]

考虑：时间、最近聊了什么、有什么想说的"""

                        messages = [{"role": "system", "content": system}]
                        history = trim_history_for_api(user.get("history", [])[-10:], current_api)
                        messages.extend(history)
                        messages.append({"role": "user", "content": "（系统：要主动说点什么吗？）"})

                        reply = call_ai(messages, current_api)

                        if "[不发]" not in reply:
                            visible, _, _ = parse_hidden_commands(reply, user_id)
                            if visible.strip():
                                send_slack(channel, visible)
                                log_message(channel, "assistant", f"[主动] {visible}", model="AI")
                                user["last_active"] = now.timestamp()
                                print(f"[Scheduler] 主动发消息给 {user_id}")

                schedules[user_id] = user_schedules

            save_schedules(schedules)
            save_user_data(all_data)

        except Exception as e:
            print(f"[Scheduler] 出错: {str(e)}")

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

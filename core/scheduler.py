"""L6: 自主行动引擎 — 定时任务 + 每日摘要 + 会议归档 + 任务提醒 + 定时 agent 任务。"""
import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from ..config import SCHEDULER_CONFIG_FILE
from ..feishu.client import get_calendar_agenda, get_my_tasks, search_meetings, get_meeting_minute_token, get_meeting_notes, create_document
from .memory import memory

DEFAULT_SCHEDULER_CONFIG = {
    "enabled": True,
    "daily_summary": {"enabled": True, "hour": 9, "minute": 0, "chat_id": None},
    "meeting_auto_archive": {"enabled": True, "interval_minutes": 60},
    "task_reminder": {"enabled": True, "interval_hours": 3},
    "agent_jobs": [],  # 定时 agent 任务，见 _run_agent_job
    "last_run": {},
}

# agent_jobs 条目示例（每周五 17:00 自动生成更新日志并推送到群）：
# {
#   "name": "weekly_changelog", "enabled": true,
#   "profile": "dev", "chat_id": "oc_目标群",
#   "day_of_week": 4, "hour": 17, "minute": 0,
#   "prompt": "用 p4 changes 拉取本周提交，生成更新日志（新功能/修复/优化分类），"
#             "用 lark-cli docs +create 归档到知识库，最终回复文档链接。"
# }

# configure() 返回此标记表示「把当前群设为推送目标」，由消息处理器注入 chat_id
PUSH_TARGET_MARKER = "PUSH_TARGET_HERE"

class Scheduler:
    """轻量定时任务引擎，基于 threading.Timer。

    支持：
    - 每日定时任务（如早上9点推送）
    - 间隔任务（如每60分钟检查会议）
    - 用户通过聊天配置
    """

    def __init__(self):
        self.config = self._load_config()
        self.timers: list[threading.Timer] = []
        self.running = False
        self._push_callback = None  # 外部设置的推送函数

    def _load_config(self) -> dict:
        try:
            if SCHEDULER_CONFIG_FILE.exists():
                with open(SCHEDULER_CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    # 合并默认值
                    cfg = DEFAULT_SCHEDULER_CONFIG.copy()
                    cfg.update(loaded)
                    return cfg
        except Exception as e:
            print(f"[SCHEDULER] config load error: {e}", flush=True)
        return DEFAULT_SCHEDULER_CONFIG.copy()

    def _save_config(self) -> None:
        try:
            with open(SCHEDULER_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[SCHEDULER] config save error: {e}", flush=True)

    def set_push_callback(self, callback) -> None:
        """设置推送回调：callback(chat_id, text)。"""
        self._push_callback = callback

    def _push(self, chat_id: str, text: str) -> None:
        """推送到指定 chat。"""
        if self._push_callback:
            try:
                self._push_callback(chat_id, text)
                print(f"[SCHEDULER] pushed to {chat_id}: {text[:80]}...", flush=True)
            except Exception as e:
                print(f"[SCHEDULER] push error: {e}", flush=True)
        else:
            print(f"[SCHEDULER] (no callback) would push: {text[:100]}...", flush=True)

    def _seconds_until(self, hour: int, minute: int) -> float:
        """计算距离下一个指定时间的秒数。"""
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target.replace(day=now.day + 1)
        return (target - now).total_seconds()

    def _seconds_until_weekly(self, day_of_week: int, hour: int, minute: int) -> float:
        """计算距离下一个星期几 hh:mm 的秒数（day_of_week: 0=周一 … 6=周日）。"""
        now = datetime.now()
        days_ahead = (day_of_week - now.weekday()) % 7
        target = (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=7)
        return (target - now).total_seconds()

    def _run_agent_job(self, job: dict) -> None:
        """执行一个定时 agent 任务：runtime 跑 prompt，结果推送到目标群。"""
        if not job.get("enabled"):
            return
        name = job.get("name", "unnamed")
        print(f"[SCHEDULER] running agent job: {name}", flush=True)
        try:
            from ..agent import runtime  # 延迟导入，避免模块级依赖
            text, stats = runtime.run(
                f"sched_{name}", job.get("prompt", ""),
                sender_id="", profile=job.get("profile", "dev"))
            chat_id = job.get("chat_id")
            if chat_id:
                self._push(chat_id, text)
            self.config["last_run"][f"agent_job_{name}"] = int(datetime.now().timestamp())
            self._save_config()
            print(f"[SCHEDULER] agent job {name} done: tools={stats['tools']}", flush=True)
        except Exception as e:
            print(f"[SCHEDULER] agent job {name} error: {e}", flush=True)

    def _run_daily_summary(self) -> None:
        """生成每日摘要并推送。"""
        if not self.config["daily_summary"]["enabled"]:
            return
        chat_id = self.config["daily_summary"].get("chat_id")
        if not chat_id:
            return

        print(f"[SCHEDULER] generating daily summary...", flush=True)
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            parts = [f"## ☀️ {today} 每日站会摘要\n"]

            # 日程
            agenda = get_calendar_agenda(today, today)
            if agenda:
                parts.append("### 📅 今日日程")
                for item in agenda[:5]:
                    summary = item.get("summary", item.get("subject", "未知"))
                    start = item.get("start_time", item.get("start", ""))
                    parts.append(f"- {start} {summary}")
            else:
                parts.append("### 📅 今日日程\n- 暂无安排")

            # 任务
            tasks = get_my_tasks()
            if tasks:
                parts.append("\n### 📋 待办任务")
                for t in tasks[:5]:
                    title = t.get("summary", t.get("title", str(t)))
                    due = t.get("due", t.get("deadline", ""))
                    due_str = f" (截止: {due})" if due else ""
                    parts.append(f"- {title}{due_str}")
            else:
                parts.append("\n### 📋 待办任务\n- 暂无任务")

            # 记忆库最新
            recent = memory.recall("项目进展 进度", top_k=3)
            if recent:
                parts.append("\n### 🧠 近期项目动态")
                for r in recent:
                    parts.append(f"- {r['content']}")

            summary = "\n".join(parts)
            self._push(chat_id, summary)
            self.config["last_run"]["daily_summary"] = int(datetime.now().timestamp())
            self._save_config()
        except Exception as e:
            print(f"[SCHEDULER] daily summary error: {e}", flush=True)

    def _run_meeting_archive_check(self) -> None:
        """检查新会议并自动归档。"""
        if not self.config["meeting_auto_archive"]["enabled"]:
            return

        print(f"[SCHEDULER] checking for meetings to archive...", flush=True)
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            yesterday = datetime.fromtimestamp(
                datetime.now().timestamp() - 86400
            ).strftime("%Y-%m-%d")

            meetings = search_meetings(yesterday, today)
            for meeting in meetings[:3]:
                meeting_id = meeting.get("id", meeting.get("meeting_id", ""))
                topic = meeting.get("topic", meeting.get("subject", meeting_id))
                if not meeting_id:
                    continue

                # 检查是否已经归档过
                last_archived = self.config["last_run"].get(f"archived_{meeting_id}")
                if last_archived:
                    continue

                minute_token = get_meeting_minute_token(meeting_id)
                if not minute_token:
                    continue

                notes = get_meeting_notes(minute_token)
                if not notes or notes.startswith("["):
                    continue

                # 用简单的规则总结（避免额外 Claude 调用）
                summary = f"# 会议纪要: {topic}\n\n日期: {today}\n\n{notes[:3000]}"
                create_document(f"会议纪要 - {topic} - {today}", summary)
                self.config["last_run"][f"archived_{meeting_id}"] = int(
                    datetime.now().timestamp()
                )
                self._save_config()
                print(f"[SCHEDULER] archived meeting: {topic}", flush=True)

        except Exception as e:
            print(f"[SCHEDULER] meeting archive error: {e}", flush=True)

    def _schedule_daily(self, task_name: str, hour: int, minute: int, task_fn) -> None:
        """安排每日定时任务。"""
        delay = self._seconds_until(hour, minute)
        print(f"[SCHEDULER] {task_name} scheduled in {delay/3600:.1f}h", flush=True)

        def wrapper():
            if not self.running:
                return
            task_fn()
            # 重新安排明天的
            self._schedule_daily(task_name, hour, minute, task_fn)

        timer = threading.Timer(delay, wrapper)
        timer.daemon = True
        timer.start()
        self.timers.append(timer)

    def _schedule_weekly(self, task_name: str, day_of_week: int,
                         hour: int, minute: int, task_fn) -> None:
        """安排每周定时任务。"""
        delay = self._seconds_until_weekly(day_of_week, hour, minute)
        print(f"[SCHEDULER] {task_name} scheduled in {delay/86400:.1f}d", flush=True)

        def wrapper():
            if not self.running:
                return
            task_fn()
            self._schedule_weekly(task_name, day_of_week, hour, minute, task_fn)

        timer = threading.Timer(delay, wrapper)
        timer.daemon = True
        timer.start()
        self.timers.append(timer)

    def _schedule_interval(self, task_name: str, interval_seconds: float, task_fn) -> None:
        """安排间隔任务。"""
        print(f"[SCHEDULER] {task_name} every {interval_seconds/60:.0f}min", flush=True)

        def wrapper():
            if not self.running:
                return
            task_fn()
            # 重新安排
            self._schedule_interval(task_name, interval_seconds, task_fn)

        timer = threading.Timer(interval_seconds, wrapper)
        timer.daemon = True
        timer.start()
        self.timers.append(timer)

    def start(self) -> None:
        """启动所有定时任务。"""
        if self.running:
            return
        self.running = True
        print("[SCHEDULER] starting autonomous agent...", flush=True)

        cfg = self.config

        # 每日摘要
        if cfg["daily_summary"]["enabled"]:
            h = cfg["daily_summary"].get("hour", 9)
            m = cfg["daily_summary"].get("minute", 0)
            self._schedule_daily("daily_summary", h, m, self._run_daily_summary)

        # 会议自动归档
        if cfg["meeting_auto_archive"]["enabled"]:
            interval = cfg["meeting_auto_archive"].get("interval_minutes", 60) * 60
            self._schedule_interval("meeting_archive", interval,
                                    self._run_meeting_archive_check)

        # 任务提醒（首次延迟后每 N 小时）
        if cfg["task_reminder"]["enabled"]:
            interval = cfg["task_reminder"].get("interval_hours", 3) * 3600
            # 首次延迟 5 分钟让 bot 先启动
            self._schedule_interval("task_reminder", interval, self._run_task_reminder)

        # 定时 agent 任务（如每周更新日志）
        for job in cfg.get("agent_jobs", []):
            if job.get("enabled"):
                self._schedule_weekly(
                    f"agent_job_{job.get('name', 'unnamed')}",
                    job.get("day_of_week", 4), job.get("hour", 17), job.get("minute", 0),
                    lambda j=job: self._run_agent_job(j))

        print("[SCHEDULER] all tasks armed", flush=True)

    def stop(self) -> None:
        """停止所有定时任务。"""
        self.running = False
        for t in self.timers:
            t.cancel()
        self.timers.clear()
        print("[SCHEDULER] stopped", flush=True)

    def _restart_if_running(self) -> None:
        """配置变更后即时重排定时任务（调度器未运行则留待下次启动生效）。"""
        if self.running and self._push_callback:
            self.stop()
            self.start()
            print("[SCHEDULER] restarted to apply config change", flush=True)

    def _run_task_reminder(self) -> None:
        """检查任务截止日期并推送提醒。"""
        if not self.config["task_reminder"]["enabled"]:
            return

        chat_id = self.config["daily_summary"].get("chat_id")
        if not chat_id:
            return

        try:
            tasks = get_my_tasks()
            if not tasks:
                return

            now = datetime.now()
            overdue = []
            upcoming = []

            for t in tasks:
                due_str = str(t.get("due", t.get("deadline", t.get("due_date", ""))))
                if not due_str or due_str == "None":
                    continue
                try:
                    # 尝试多种日期格式
                    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"]:
                        try:
                            due_date = datetime.strptime(due_str[:10], fmt[:10])
                            break
                        except ValueError:
                            continue
                    else:
                        continue

                    days_left = (due_date - now).days
                    title = t.get("summary", t.get("title", str(t)[:50]))
                    if days_left < 0:
                        overdue.append(f"- ⚠️ **逾期**: {title} (截止: {due_str[:10]})")
                    elif days_left <= 1:
                        upcoming.append(
                            f"- 🔴 **今天截止**: {title}" if days_left == 0
                            else f"- 🟡 **明天截止**: {title}"
                        )
                except Exception:
                    continue

            if overdue or upcoming:
                msg = "## 📢 任务提醒\n\n"
                if overdue:
                    msg += "\n".join(overdue) + "\n\n"
                if upcoming:
                    msg += "\n".join(upcoming)
                self._push(chat_id, msg)

        except Exception as e:
            print(f"[SCHEDULER] task reminder error: {e}", flush=True)

    def configure(self, command: str) -> str:
        """处理用户通过聊天发送的调度配置命令。

        支持的命令:
        - 开启/关闭每日推送
        - 设置推送时间
        - 开启/关闭会议自动归档
        - 设置推送目标群
        - 查看当前配置
        """
        cmd = command.strip().lower()

        # 查看配置
        if any(kw in cmd for kw in ["查看配置", "当前配置", "调度状态", "schedule status"]):
            lines = ["## ⚙️ 调度配置"]
            lines.append(f"- 调度引擎: {'✅ 运行中' if self.running else '❌ 已停止'}")
            lines.append(f"- 每日摘要: {'✅' if self.config['daily_summary']['enabled'] else '❌'} "
                        f"({self.config['daily_summary']['hour']:02d}:"
                        f"{self.config['daily_summary']['minute']:02d})")
            chat_id = self.config['daily_summary'].get('chat_id', '未设置')
            lines.append(f"- 推送目标: {chat_id}")
            lines.append(f"- 会议自动归档: "
                        f"{'✅' if self.config['meeting_auto_archive']['enabled'] else '❌'} "
                        f"(每{self.config['meeting_auto_archive']['interval_minutes']}分钟)")
            lines.append(f"- 任务提醒: "
                        f"{'✅' if self.config['task_reminder']['enabled'] else '❌'} "
                        f"(每{self.config['task_reminder']['interval_hours']}小时)")
            jobs = [j for j in self.config.get("agent_jobs", []) if j.get("enabled")]
            lines.append(f"- 定时 agent 任务: {len(jobs)} 个启用"
                        + (f"（{', '.join(j.get('name','?') for j in jobs)}）" if jobs else ""))
            return "\n".join(lines)

        # 设定推送目标
        if "推送目标" in cmd or "推送到这里" in cmd:
            # 从上下文获取 chat_id 在调用时注入
            return PUSH_TARGET_MARKER  # 特殊标记，在 process_message 中处理

        # 开启/关闭每日推送
        if "关闭每日推送" in cmd or "关掉每日推送" in cmd:
            self.config["daily_summary"]["enabled"] = False
            self._save_config()
            self._restart_if_running()
            return "已关闭每日摘要推送。"

        if "开启每日推送" in cmd or "打开每日推送" in cmd:
            self.config["daily_summary"]["enabled"] = True
            self._save_config()
            self._restart_if_running()
            return "已开启每日摘要推送。"

        # 设置推送时间
        time_match = re.search(r'(?:推送时间|定时推送).*?(\d{1,2})[:：](\d{2})', cmd)
        if time_match:
            h, m = int(time_match.group(1)), int(time_match.group(2))
            if 0 <= h <= 23 and 0 <= m <= 59:
                self.config["daily_summary"]["hour"] = h
                self.config["daily_summary"]["minute"] = m
                self._save_config()
                self._restart_if_running()
                return f"每日推送时间已设为 {h:02d}:{m:02d}，已即时生效。"

        # 开启/关闭会议自动归档
        if "关闭会议归档" in cmd or "关掉会议归档" in cmd:
            self.config["meeting_auto_archive"]["enabled"] = False
            self._save_config()
            self._restart_if_running()
            return "已关闭会议自动归档。"

        if "开启会议归档" in cmd or "打开会议归档" in cmd:
            self.config["meeting_auto_archive"]["enabled"] = True
            self._save_config()
            self._restart_if_running()
            return "已开启会议自动归档。"

        # 开启/关闭任务提醒
        if "关闭任务提醒" in cmd:
            self.config["task_reminder"]["enabled"] = False
            self._save_config()
            self._restart_if_running()
            return "已关闭任务提醒。"

        if "开启任务提醒" in cmd:
            self.config["task_reminder"]["enabled"] = True
            self._save_config()
            self._restart_if_running()
            return "已开启任务提醒。"

        return ""

    def set_push_target(self, chat_id: str) -> str:
        """设置推送目标群。"""
        self.config["daily_summary"]["chat_id"] = chat_id
        self._save_config()
        return f"已将当前群设为每日推送和提醒的目标。"

# 全局调度器实例
scheduler = Scheduler()

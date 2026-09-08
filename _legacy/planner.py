"""L4: 任务计划解析与追踪。"""
import re

PLAN_PATTERN = re.compile(r'\[PLAN\]\n?(.*?)\n?\[/PLAN\]', re.DOTALL)
STEP_PATTERN = re.compile(r'步骤(\d+)[：:]\s*(.+?)(?:\s*→\s*(\w+)(?:\s+(.+))?)?\s*$', re.MULTILINE)

def parse_plan(response: str) -> tuple[list[dict], str]:
    """从响应中提取执行计划。每步骤: {num, description, tool, tool_params, status, result}。"""
    plans = []
    clean = response
    for m in PLAN_PATTERN.finditer(response):
        plan_text = m.group(1)
        clean = clean.replace(m.group(0), "")
        for step_m in STEP_PATTERN.finditer(plan_text):
            try:
                step_num = int(step_m.group(1))
            except ValueError:
                continue
            description = step_m.group(2).strip()
            tool_name = step_m.group(3).strip() if step_m.group(3) else None
            tool_params_str = step_m.group(4).strip() if step_m.group(4) else ""
            params = {}
            if tool_params_str:
                for pair in tool_params_str.split():
                    if "=" in pair:
                        k, _, v = pair.partition("=")
                        params[k.strip()] = v.strip()
                    else:
                        params["query"] = tool_params_str
            plans.append({
                "num": step_num, "description": description,
                "tool": tool_name, "tool_params": params,
                "status": "pending", "result": None,
            })
    plans.sort(key=lambda s: s["num"])
    return plans, clean.strip()

def format_plan_progress(plans: list[dict]) -> str:
    """格式化计划进度。"""
    icons = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌"}
    lines = ["\n## 📋 执行计划"]
    for step in plans:
        icon = icons.get(step["status"], "⏳")
        tool_info = f" → `{step['tool']}`" if step.get("tool") else ""
        lines.append(f"{icon} 步骤{step['num']}: {step['description']}{tool_info}")
    return "\n".join(lines)

def build_plan_context(plans: list[dict], current_step_idx: int = -1) -> str:
    """构建计划上下文，告诉 Claude 当前进度。"""
    ctx = ["[系统提示] 当前正在按照以下计划执行："]
    for i, step in enumerate(plans):
        if i == current_step_idx:
            ctx.append(f"  → 正在执行: 步骤{step['num']}: {step['description']}")
        elif step["status"] == "done":
            ctx.append(f"  ✅ 已完成: 步骤{step['num']}: {step['description']}")
            if step.get("result"):
                ctx.append(f"     结果: {str(step['result'])[:300]}")
        else:
            ctx.append(f"  ⏳ 待执行: 步骤{step['num']}: {step['description']}")
    ctx.append("\n请继续执行下一个待执行的步骤，或如果所有步骤已完成，请给出最终结果。")
    return "\n".join(ctx)

"""毕设文件读取 — 沙箱限制，仅允许在毕设目录内操作。"""
from pathlib import Path
from .config import THESIS_DIR

def _resolve_safe(relative_path: str) -> Path | None:
    """解析路径并校验不超出 THESIS_DIR 沙箱。返回 None 表示越权。"""
    if not THESIS_DIR.exists():
        return None
    raw = (THESIS_DIR / relative_path).resolve()
    try:
        raw.relative_to(THESIS_DIR.resolve())
    except ValueError:
        return None
    return raw

def list_thesis_files() -> list[Path]:
    if not THESIS_DIR.exists():
        return []
    return sorted([p for p in THESIS_DIR.rglob("*")
                   if p.is_file() and not p.name.startswith("~$")])

def read_thesis_file(relative_path: str) -> str:
    # 安全检查
    full_path = _resolve_safe(relative_path)
    if full_path is None:
        # 尝试在当前目录内模糊匹配
        for f in THESIS_DIR.rglob("*"):
            if f.is_file() and not f.name.startswith("~$"):
                if relative_path.lower() in f.name.lower():
                    full_path = f
                    break

    if full_path is None:
        return f"[访问被拒绝或文件不存在: {relative_path}]"
    if not full_path.exists() or not full_path.is_file():
        return f"[文件不存在: {relative_path}]"

    try:
        suffix = full_path.suffix.lower()
        if suffix == ".md":
            return full_path.read_text(encoding="utf-8")
        elif suffix == ".docx":
            from docx import Document
            return "\n".join(p.text for p in Document(str(full_path)).paragraphs if p.text.strip())
        elif suffix in {".py", ".json", ".txt", ".yaml", ".yml", ".toml",
                         ".cs", ".cpp", ".h", ".js", ".ts", ".html", ".css", ".xml", ".csv"}:
            return full_path.read_text(encoding="utf-8")
        elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            return f"[图片: {full_path.name} ({full_path.stat().st_size/1024:.0f}KB)]"
        else:
            return f"[不支持的类型: {suffix}]"
    except Exception as e:
        return f"[读取失败: {e}]"

def write_thesis_file(relative_path: str, content: str) -> str:
    """在毕设目录内写入文件。沙箱限制，不可越权。"""
    full_path = _resolve_safe(relative_path)
    if full_path is None:
        return f"[写入被拒绝: {relative_path} — 不允许操作毕设目录外的路径]"
    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return f"已写入: {full_path.name}"
    except Exception as e:
        return f"[写入失败: {e}]"

def get_thesis_overview() -> str:
    files = list_thesis_files()
    if not files:
        return "毕设目录为空。"
    lines = ["## 毕设项目文件"]
    for f in files:
        kind = "📄" if f.suffix in (".md", ".docx") else "🖼️" if f.suffix in (".png",".jpg") else "📁"
        lines.append(f"- {kind} `{f.name}` ({f.stat().st_size/1024:.1f}KB)")
    return "\n".join(lines)

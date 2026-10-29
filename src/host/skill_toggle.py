"""skill_toggle.py —— 桌宠菜单里的「拷问模式」开关

两套机制，缺一不可
==================

① **技能可见性**（原有）：grill-me 装在 `~/.dsh/skills/grill-me/SKILL.md`。
   DSH 静默扫描 `~/.dsh/skills` 的直接子目录，目录存在即进入模型技能目录、占用词元。
   所以"关闭"必须让目录消失：
       ~/.dsh/skills/grill-me          ←→    ~/.dsh/skills/.disabled/grill-me

② **自动进入拷问**（2026-10-29 新增）：技能只是"说明书"，DSH 不会因为
   `description` 里写了触发词就主动调用它 —— 模型只有在语义匹配时才去读。
   用户要的是"开关打开后，后续对话**自动**进入拷问"，这与"技能可见"是两件事。

   实现：在 `~/.dsh/AGENTS.md` 里维护一个**受管区块**。
   `AGENTS.md` 是每轮对话都会注入的全局规则，所以区块在 = 每轮都生效。

受管区块约定（重要）
====================
写入的规则被两个标记包住：

    <!-- dsh-pet:grill-mode:begin -->
    ...规则...
    <!-- dsh-pet:grill-mode:end -->

· 只增删**标记之间**的内容，标记之外一个字节都不碰（用户自己的语言/图片规则不受影响）；
· 幂等：重复开启不会写第二份，重复关闭不会报错；
· 关闭 = 精确删除整块（含标记），文件恢复原样；
· 用户手动改坏标记（只剩一头）时，`is_mode_active()` 返回 False 并拒绝写入，
  避免把规则插到文件中间造成语法混乱。

状态来源
========
**以磁盘为准**，不写 settings.json：
菜单状态每次由 `is_mode_active()` 现场探测 `AGENTS.md`，
即使你在外面手动改过文件，桌宠菜单也会自动跟上。
"""
import os
import re
import shutil
from pathlib import Path

SKILL_NAME = "grill-me"

# ---- 受管区块标记 ----
MARK_BEGIN = "<!-- dsh-pet:grill-mode:begin -->"
MARK_END = "<!-- dsh-pet:grill-mode:end -->"

# 区块内容：让模型在每一轮都自动进入"拷问"行为。
# 用"除非…否则…"的强约束写法，并明确它优先于"直接动手实现"的默认倾向。
MODE_RULE = """\
## 拷问模式（由桌宠菜单控制，勿手动编辑本区块）

当前处于**拷问模式**。除非用户明确说"退出拷问/别问了"或关掉桌宠菜单里的开关，
否则按以下方式工作：

- 用户提出任何**方案、设计、计划、需求、想法**时，**先不要动手实现**，
  而是用 `ask_user_question` 逐条追问，把决策树上的分支一个个问清楚。
- **一次只问一个问题**，等回答后再问下一个。每个问题给 2–4 个具体选项
  （不要用"是/否"这种空洞选项，除非问题本身就是二选一）。
- 用户回答后，用 1–2 句话确认该决定，然后立刻问下一个问题。
- **能通过读代码/查文件回答的，自己去查，不要问用户**。
- 所有分支问完后，给一份简短的决策汇总，再问用户是否开始实现。
- 用户要求"直接做/别问了"时，立即退出拷问、按普通模式执行本次任务。
"""


# DSH 用户技能根目录：优先级 400 的 `<dshHome>/skills`
# dshHome 默认 ~/.dsh，也支持 $DSH_HOME 覆盖（与 DSH 自身的解析顺序一致）
def dsh_home() -> Path:
    home = os.environ.get("DSH_HOME")
    if home:
        return Path(home)
    return Path.home() / ".dsh"


def dsh_skills_root() -> Path:
    return dsh_home() / "skills"


def agents_path() -> Path:
    """全局规则文件（每轮注入）。"""
    return dsh_home() / "AGENTS.md"


def skill_dir(root: Path = None) -> Path:
    """启用状态下的技能目录。"""
    return (root or dsh_skills_root()) / SKILL_NAME


def disabled_dir(root: Path = None) -> Path:
    """停用状态下的存放位置（`skills/.disabled/<name>`）。"""
    return (root or dsh_skills_root()) / ".disabled" / SKILL_NAME


def is_installed(root: Path = None) -> bool:
    """技能是否存在于本机（启用或停用任一状态）。"""
    return skill_dir(root).is_dir() or disabled_dir(root).is_dir()


def is_enabled(root: Path = None, agents: Path = None) -> bool:
    """拷问模式是否**完全生效**：技能目录可见 **且** AGENTS.md 有受管区块。

    ★ 为什么必须是"且"（2026-10-29）：
      只搬目录时，菜单显示"开"，但模型并不会自动拷问 ——
      用户看到的就是"开了却从来没生效过"。
      所以"开"的定义必须包含 AGENTS.md 区块，否则状态会骗人。
    """
    return skill_dir(root).is_dir() and is_mode_active(agents)


# ---------------------------------------------------------------- AGENTS.md
def _read_agents(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def is_mode_active(agents: Path = None) -> bool:
    """拷问模式是否生效 = AGENTS.md 里有**完整**的受管区块。"""
    p = agents or agents_path()
    txt = _read_agents(p)
    if not txt:
        return False
    i = txt.find(MARK_BEGIN)
    j = txt.find(MARK_END)
    return i != -1 and j != -1 and j > i


def _block() -> str:
    return f"{MARK_BEGIN}\n{MODE_RULE}{MARK_END}\n"


def set_mode(enabled: bool, agents: Path = None) -> bool:
    """在 AGENTS.md 里增/删拷问模式区块。返回是否成功。

    安全边界：
      · 标记之外的原有内容**原样保留**（按行拼接，不重排、不改换行风格）；
      · 只剩一头标记时视为文件已损坏 → 拒绝写入，返回 False（不猜用户意图）；
      · 原子写（临时文件 + os.replace），失败静默不影响桌宠。
    """
    p = agents or agents_path()
    try:
        txt = _read_agents(p)
        has_begin = MARK_BEGIN in txt
        has_end = MARK_END in txt

        if enabled:
            if has_begin and has_end:
                return True                     # 已开启，幂等
            if has_begin or has_end:
                return False                    # 标记残缺，拒绝写入
            sep = "" if (not txt or txt.endswith("\n")) else "\n"
            new = txt + sep + "\n" + _block()
        else:
            if not has_begin and not has_end:
                return True                     # 已关闭，幂等
            if not (has_begin and has_end):
                return False                    # 标记残缺，拒绝猜测
            # 精确删除 [begin, end] 整块，含其后的换行
            pat = re.compile(
                r"\n*" + re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?",
                re.S)
            new = pat.sub("\n", txt, count=1)
            new = re.sub(r"\n{3,}", "\n\n", new)   # 收掉多余空行

        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 对外接口
def set_enabled(enabled: bool, root: Path = None, agents: Path = None) -> bool:
    """启用 / 停用拷问模式：**同时**处理技能目录与 AGENTS.md 区块。

    为什么要两处一起动：
      · 目录：决定 DSH 能不能"看见"这份技能说明书；
      · AGENTS.md：决定模型会不会**自动**按它做（这才是用户要的"开关即模式"）。
    只动目录 → 表现为"从来没生效过，除非特意去说"（用户 2026-10-29 报的问题）。

    ★ 原子性：开启时**先确认技能目录可用**，再写 AGENTS.md。
      否则会出现"目录搬不动但规则已写入"的半开状态 —— 那比不做更糟：
      模型被规则要求拷问，但技能说明书根本不在。
      关闭时顺序相反：先删规则，再挪目录（宁可少一条规则，不要留半截）。

    幂等：已经是目标状态时直接返回 True。失败静默，不影响桌宠运行。
    """
    if enabled:
        if not _set_skill_dir(True, root):
            return False                     # 技能不可用 → 一个字节都别写
        return set_mode(True, agents)
    # 关闭：先撤规则（保证模型立刻停止拷问），再挪目录
    ok_rule = set_mode(False, agents)
    ok_dir = _set_skill_dir(False, root)
    return ok_rule and ok_dir


def _set_skill_dir(enabled: bool, root: Path = None) -> bool:
    """只动技能目录（保留原 `set_enabled` 的语义，供旧测试/旧调用使用）。"""
    try:
        on, off = skill_dir(root), disabled_dir(root)
        if enabled:
            if on.is_dir():
                return True                      # 已启用
            if not off.is_dir():
                return False                     # 根本不存在
            on.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(off), str(on))
            return True
        else:
            if off.is_dir():
                return True                      # 已停用
            if not on.is_dir():
                return False                     # 不存在
            off.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(on), str(off))
            return True
    except Exception:
        return False


def toggle(root: Path = None, agents: Path = None) -> bool:
    """切换开关，返回切换后的状态（失败时返回切换前的状态）。"""
    before = is_mode_active(agents)
    ok = set_enabled(not before, root, agents)
    return is_mode_active(agents) if ok else before


def menu_label(root: Path = None, agents: Path = None) -> str:
    """菜单文字：名称即状态，点击切换（与「行为：群体/单体」「摇晃：开/关」同构）。

    ★ 统一用「名称：状态」的冒号写法，不再用括号 —— 与同菜单里其它格一致。

    ★ "开" = 目录在 **且** AGENTS.md 有区块（见 is_enabled 的说明）。
      只满足一半时显示"开·未生效"，让状态不骗人，也方便排查。
    """
    if not is_installed(root):
        return "DSH拷问：未安装"
    dir_on = skill_dir(root).is_dir()
    rule_on = is_mode_active(agents)
    if dir_on and rule_on:
        return "DSH拷问：开"
    if dir_on or rule_on:
        return "DSH拷问：开·未生效"     # 半开：能点一次修正
    return "DSH拷问：关"

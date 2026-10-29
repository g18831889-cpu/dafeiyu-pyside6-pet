"""验证「拷问模式」开关（skill_toggle）。

两套机制都要测：
  ① 技能目录搬运：`skills/grill-me` ↔ `skills/.disabled/grill-me`
  ② AGENTS.md 受管区块：增 / 删 / 幂等 / 不碰区块外内容 / 标记残缺时拒绝写入

不碰真实 ~/.dsh：所有用例都在临时目录里跑（root 与 agents 都显式传入）。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from src.host import skill_toggle as st

results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ''))


tmp = Path(tempfile.mkdtemp())
root = tmp / 'skills'
root.mkdir(parents=True)
agents = tmp / 'AGENTS.md'

# 用户原有的 AGENTS.md 内容（区块之外，一个字节都不能动）
ORIGINAL = """# 全局规则

## 语言

- 面向用户的回复一律使用简体中文。

## 展示图片

- 涉及图片时用 Markdown 渲染。
"""
agents.write_text(ORIGINAL, encoding='utf-8')

# ============ 0) 未安装 ============
check('not installed initially', st.is_installed(root) is False)
check('not enabled initially', st.is_enabled(root, agents) is False)
check('mode inactive initially', st.is_mode_active(agents) is False)
check('set_enabled on missing returns False', st.set_enabled(True, root, agents) is False)
check('label says missing', '未安装' in st.menu_label(root, agents))


# ============ 1) 造一个技能 ============
def make_skill():
    d = st.skill_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / 'SKILL.md').write_text('---\nname: grill-me\n---\nbody\n', encoding='utf-8')


make_skill()
check('installed after create', st.is_installed(root) is True)
check('dir visible but mode not yet on', st.is_enabled(root, agents) is False)
check('live path is skills/grill-me', st.skill_dir(root) == root / 'grill-me')
# 目录在但 AGENTS.md 没区块 → 菜单不能显示"开"，否则就是用户遇到的"开了却没生效"
check('label flags half-on when only dir exists',
      st.menu_label(root, agents) == 'DSH拷问：开·未生效', st.menu_label(root, agents))
check('is_enabled False when rule missing', st.is_enabled(root, agents) is False)

# ============ 2) 开启：目录 + 区块同时到位 ============
check('enable returns True', st.set_enabled(True, root, agents) is True)
check('enabled: full mode on', st.is_enabled(root, agents) is True)
check('enabled: mode active', st.is_mode_active(agents) is True)
check('label reads ON', st.menu_label(root, agents) == 'DSH拷问：开',
      st.menu_label(root, agents))

txt = agents.read_text(encoding='utf-8')
check('block has begin marker', st.MARK_BEGIN in txt)
check('block has end marker', st.MARK_END in txt)
check('block mentions ask_user_question', 'ask_user_question' in txt)
check('block asks one question at a time', '一次只问一个问题' in txt)
check('original content preserved (语言)', '- 面向用户的回复一律使用简体中文。' in txt)
check('original content preserved (图片)', '- 涉及图片时用 Markdown 渲染。' in txt)
check('original heading preserved', txt.startswith('# 全局规则'))

# ============ 3) 幂等：重复开启 ============
before = agents.read_text(encoding='utf-8')
check('enable twice is idempotent', st.set_enabled(True, root, agents) is True)
check('enable twice does not duplicate block',
      agents.read_text(encoding='utf-8').count(st.MARK_BEGIN) == 1)

# ============ 4) 关闭：目录移走 + 区块删除 ============
check('disable returns True', st.set_enabled(False, root, agents) is True)
check('disabled: not enabled', st.is_enabled(root, agents) is False)
check('disabled: mode inactive', st.is_mode_active(agents) is False)
check('label reads OFF', st.menu_label(root, agents) == 'DSH拷问：关',
      st.menu_label(root, agents))
check('disabled: still installed', st.is_installed(root) is True)
check('disabled: live dir gone', not (root / 'grill-me').exists())
check('disabled: parked under .disabled', (root / '.disabled' / 'grill-me').is_dir())
check('disabled: SKILL.md preserved',
      (root / '.disabled' / 'grill-me' / 'SKILL.md').is_file())
check('.disabled invisible to direct-child scan',
      not any(p.name == 'grill-me' for p in root.iterdir() if p.is_dir()))

after = agents.read_text(encoding='utf-8')
check('markers fully removed', st.MARK_BEGIN not in after and st.MARK_END not in after)
check('AGENTS.md restored to original', after == ORIGINAL,
      repr(after[:80]) if after != ORIGINAL else '')

# ============ 5) 幂等：重复关闭 ============
check('disable twice is idempotent', st.set_enabled(False, root, agents) is True)
check('disable twice keeps original', agents.read_text(encoding='utf-8') == ORIGINAL)

# ============ 6) toggle 往返 ============
check('toggle on', st.toggle(root, agents) is True)
check('toggle sees mode active', st.is_mode_active(agents) is True)
check('toggle off', st.toggle(root, agents) is False)
check('toggle sees mode inactive', st.is_mode_active(agents) is False)

# ============ 7) 标记残缺 → 拒绝写入，不破坏文件 ============
broken = tmp / 'AGENTS-broken.md'
broken.write_text(ORIGINAL + "\n" + st.MARK_BEGIN + "\n半截区块\n", encoding='utf-8')
snapshot = broken.read_text(encoding='utf-8')
check('broken markers -> mode inactive', st.is_mode_active(broken) is False)
check('broken markers -> enable refused', st.set_mode(True, broken) is False)
check('broken markers -> file untouched', broken.read_text(encoding='utf-8') == snapshot)
check('broken markers -> disable refused too', st.set_mode(False, broken) is False)
check('broken markers -> still untouched', broken.read_text(encoding='utf-8') == snapshot)

# ============ 8) AGENTS.md 不存在时能创建 ============
fresh = tmp / 'sub' / 'AGENTS.md'
check('missing AGENTS.md -> mode inactive', st.is_mode_active(fresh) is False)
check('missing AGENTS.md -> can enable', st.set_mode(True, fresh) is True)
check('created file has block', st.is_mode_active(fresh) is True)
check('missing AGENTS.md -> can disable', st.set_mode(False, fresh) is True)
check('created file cleaned', st.MARK_BEGIN not in fresh.read_text(encoding='utf-8'))

# ============ 9) 外部手动改 AGENTS.md → 探测到状态 ============
external = tmp / 'AGENTS-ext.md'
external.write_text(ORIGINAL, encoding='utf-8')
st.set_mode(True, external)
check('external enable detected', st.is_mode_active(external) is True)
external.write_text(ORIGINAL, encoding='utf-8')      # 模拟用户手动删掉区块
check('external removal detected', st.is_mode_active(external) is False)

# ============ 10) DSH_HOME 覆盖 ============
old = os.environ.get('DSH_HOME')
os.environ['DSH_HOME'] = str(Path(tempfile.mkdtemp()))
custom_skills = st.dsh_skills_root()
custom_agents = st.agents_path()
check('DSH_HOME honored (skills)',
      custom_skills == Path(os.environ['DSH_HOME']) / 'skills', str(custom_skills))
check('DSH_HOME honored (agents)',
      custom_agents == Path(os.environ['DSH_HOME']) / 'AGENTS.md', str(custom_agents))
if old is None:
    os.environ.pop('DSH_HOME', None)
else:
    os.environ['DSH_HOME'] = old

check('default skills root is ~/.dsh/skills',
      st.dsh_skills_root() == Path.home() / '.dsh' / 'skills')
check('default agents path is ~/.dsh/AGENTS.md',
      st.agents_path() == Path.home() / '.dsh' / 'AGENTS.md')

passed = sum(1 for _, c, _ in results if c)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)

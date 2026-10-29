"""src/shared/pricing.py - DeepSeek 峰谷计价档位（与 dsh-pet 的 deepseekPricingTier 同口径）

规则（北京时间）：
  · 高峰：工作日 9:00–12:00、14:00–18:00
  · 其余：低谷（空闲价）
  · 周六/周日全天按低谷计费（不再区分峰谷）

对照 dsh-pet：`src/host/balance/deepseek-common.ts` 的 `deepseekPricingTier`。
那边在**取数时刻**算好随数据带出；pet-pyside6 的余额来自插件推送，
插件侧（复制自 dafeiyu 的 balance.js）不带 tier，所以这里在**展示时刻**自己算 ——
偏差最多 1 小时级切换点的那一下，可接受。
"""
from datetime import datetime, timedelta, timezone

# 北京时间（UTC+8）；不依赖系统时区设置，避免用户机器时区不是 +8 时算错
CST = timezone(timedelta(hours=8))


def deepseek_pricing_tier(now=None):
    """返回 'peak' 或 'idle'。"""
    dt = now or datetime.now(CST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    else:
        dt = dt.astimezone(CST)
    if dt.weekday() >= 5:  # 5=周六 6=周日
        return 'idle'
    hour = dt.hour
    return 'peak' if (9 <= hour < 12) or (14 <= hour < 18) else 'idle'


def tier_label(tier):
    """'peak' → 峰，'idle' → 谷（与 pet 的文案一致）。"""
    return '峰' if tier == 'peak' else '谷'


def balance_text(amount, tier=None):
    """余额显示文案：`余额（峰）¥12.34` / `余额（谷）¥12.34`；没有档位时退化成 `余额 ¥12.34`。"""
    if tier not in ('peak', 'idle'):
        return '余额 ¥%.2f' % amount
    return '余额（%s）¥%.2f' % (tier_label(tier), amount)


def balance_html(amount, tier=None):
    """气泡用的富文本版：峰/谷 是**加粗灰字**（对齐 pet 的 .pet-bub-tier，两档同色 #9aa0a8）。

    QLabel 默认 AutoText，含标签时会按富文本渲染。
    """
    if tier not in ('peak', 'idle'):
        return '余额 ¥%.2f' % amount
    return ('余额（<span style="color:#9aa0a8;font-weight:700">%s</span>）¥%.2f'
            % (tier_label(tier), amount))

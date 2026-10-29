// 包入口：DSH 插件加载器按 package.json 的 main 引入本文件。
// 所有外部运行时依赖都放在下面的动态 import 后面 —— 静态 import 在依赖缺失时会抛
// ERR_MODULE_NOT_FOUND，把整棵插件树拖崩；这里退化成"惰性插件 + 控制台提示"，
// DSH 与其他插件照常启动。
let plugin
try {
  plugin = await import('./plugin.js')
} catch (error) {
  const detail = error instanceof Error ? error.message : String(error)
  console.error(
    '[dsh-pet-pyside6] 插件依赖加载失败，本次已自动停用（不影响 DSH 其他功能）。\n'
    + `[dsh-pet-pyside6] 原因: ${detail}\n`
    + '[dsh-pet-pyside6] 修复: 补齐依赖后完全重启 DSH。',
  )
  plugin = { name: 'dsh-pet-pyside6', inject: [], apply() {} }
}

export const name = plugin.name
export const inject = plugin.inject
export const Config = plugin.Config
export const BALANCE_ENDPOINT = plugin.BALANCE_ENDPOINT

export function apply(ctx, config = {}) {
  return plugin.apply(ctx, config)
}

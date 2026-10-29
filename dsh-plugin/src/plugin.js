/**
 * pet-pyside6 的 DSH 插件。
 *
 * 干三件事：
 *  1. 把 PySide6 桌宠**拉起来**（若已经在跑则复用，不重复拉起）
 *  2. 把 DSH 的会话事件转发给桌宠（POST 到它自己的 /event）
 *  3. 查询余额并推给桌宠（余额逻辑复用 dafeiyu 那套：credentials / account 两个来源）
 *
 * 桌宠进程本身是"独立可运行"的（自带 HTTP 3080），所以本插件**不拥有**它：
 * 只负责发现/启动/喂数据，退出时也不强杀已经在跑的实例（除非是本插件拉起的）。
 */
import { spawn, spawnSync } from 'node:child_process'
import { appendFileSync, existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { isLocalRequest, jsonResponse } from './local-http.js'
import {
  BALANCE_ENDPOINT,
  createBalanceQuery,
  createBalanceMonitor,
  createBalanceHandler,
} from './balance.js'

// schemastery 是可选依赖：本地目录安装（link）时它可能不在包的解析链上，
// 缺了就退化成"无 Config 校验"，插件功能照常。
let Schema = null
try {
  Schema = (await import('@deepseek-ai/schemastery')).default
} catch {
  Schema = null
}

export const name = 'dsh-pet-pyside6'
export const inject = ['sessions', 'settings']
export { BALANCE_ENDPOINT }

// 工程根目录：默认取本包的上一级（即 pet-pyside6/），这样从任意路径安装都成立。
// 可用环境变量 DSH_PET_PYSIDE6_DIR 覆盖（发布到 npm 后源码不在包旁边时使用）。
const PACKAGE_DIR = dirname(fileURLToPath(import.meta.url))
const DEFAULT_PROJECT = process.env.DSH_PET_PYSIDE6_DIR || join(PACKAGE_DIR, '..', '..')

export const Config = Schema ? Schema.object({
  enabled: Schema.boolean().default(true).description('启用 PySide6 桌宠'),
  autoStart: Schema.boolean().default(true).description('DSH 启动时自动拉起桌宠（已在运行则复用）'),
  projectDir: Schema.string().default(DEFAULT_PROJECT).description('pet-pyside6 工程目录（含 main.py）'),
  pythonPath: Schema.string().default('').description('python 路径（留空则用 PATH 里的 python）'),
  port: Schema.number().default(3080).description('桌宠本地 HTTP 端口'),
  forwardEvents: Schema.boolean().default(true).description('把 DSH 会话事件转发给桌宠'),
  balanceEnabled: Schema.boolean().default(true).description('查询并推送 DeepSeek 余额'),
  balanceSource: Schema.union(['auto', 'api-key', 'account']).default('auto').description('余额来源'),
}).description('PySide6 桌宠（pet-pyside6）宿主插件') : undefined

/** 探测桌宠是否已经在跑（它自带 GET /state）。 */
async function probe(port, timeoutMs = 700) {
  try {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), timeoutMs)
    const res = await fetch(`http://127.0.0.1:${port}/state`, { signal: ctrl.signal })
    clearTimeout(timer)
    return res.ok
  } catch {
    return false
  }
}

/** 把事件推给桌宠。失败只记日志——桌宠挂了不该影响 DSH。 */
async function push(port, payload, logger) {
  try {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), 1500)
    const res = await fetch(`http://127.0.0.1:${port}/event`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    })
    clearTimeout(timer)
    if (!res.ok) logger.warn?.(`[dsh-pet-pyside6] 事件推送返回 ${res.status}`)
  } catch (error) {
    logger.warn?.(`[dsh-pet-pyside6] 事件推送失败: ${error instanceof Error ? error.message : String(error)}`)
  }
}

/**
 * 把诊断信息写到 <projectDir>/logs/plugin.log。
 *
 * DSH 的 ctx.logger 输出在 GUI/主进程日志里不好找，出问题时只能靠猜。
 * 这里直接落文件，能看清 ensureRunning 到底走了哪个分支。
 */
function makeFileLog(projectDir) {
  let p = null
  try {
    p = join(projectDir, 'logs', 'plugin.log')
  } catch {
    p = null
  }
  return (msg) => {
    if (!p) return
    try {
      mkdirSync(dirname(p), { recursive: true })
      appendFileSync(p, `[${new Date().toISOString()}] ${msg}\n`, 'utf8')
    } catch {
      /* 写不了就算了 */
    }
  }
}

/** Windows 系统目录下的可执行文件绝对路径（DSH 的 PATH 可能精简过） */
function sysExe(name) {
  const root = process.env.SystemRoot || process.env.windir || 'C:\\Windows'
  return join(root, 'System32', name)
}

/**
 * 用 WMI 创建桌宠进程，使其**不继承 DSH 的 Job Object**。
 *
 * 背景：DSH host 处在一个带 KILL_ON_JOB_CLOSE 的 Job 里，普通 spawn
 * （哪怕 detached:true）出来的子进程在同一 Job，DSH 退出会被连带杀掉。
 * WMI 的 Win32_Process.Create 由 WmiPrvSE 执行创建，父进程不是 DSH，
 * 因此不在那个 Job 内（实测 IsProcessInJob = False）。
 *
 * @returns {boolean} 是否成功发起
 */
function launchViaWmi(python, mainPy, cwd, logger, flog) {
  // 解析解释器路径。两个坑：
  //  1. `where pythonw` 可能命中 Microsoft Store 的假壳
  //     （%LOCALAPPDATA%\Microsoft\WindowsApps\pythonw.exe），那个 shim 不能真正跑，
  //     必须跳过，取后面真正的那个。
  //  2. 优先 pythonw：python.exe 是 console subsystem，会驻留一个控制台黑框；
  //     pythonw 没有控制台。和 启动桌宠.bat 的取值口径一致。
  let py = String(python || '')
  if (!py) py = 'auto'
  if (!py.includes('\\') && !py.includes('/')) {
    const cands = py.toLowerCase() === 'python' ? ['python', 'pythonw'] : ['pythonw', 'python']
    for (const name of cands) {
      try {
        const r = spawnSync(sysExe('where.exe'), [name], { encoding: 'utf8', windowsHide: true })
        const list = String(r.stdout || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
        // 跳过 Store 假壳
        const first = list.find((p) => !/\\WindowsApps\\/i.test(p))
        if (first) {
          const skipped = list.length - (first === list[0] ? 1 : list.indexOf(first))
          flog?.(`where ${name} -> ${first}` +
                 (list.indexOf(first) > 0 ? `（跳过了 ${list.indexOf(first)} 个 Store 假壳）` : ''))
          py = first
          break
        }
        flog?.(`where ${name} -> ${list.length ? '只有 Store 假壳' : '(not found)'}`)
      } catch (e) {
        flog?.(`where ${name} failed: ${e.message}`)
      }
    }
  }
  const cmdLine = `"${py}" -u "${mainPy}"`
  // PowerShell 单引号字符串：内部单引号要写成两个
  const q = (s) => "'" + String(s).replace(/'/g, "''") + "'"
  // ★ 不要加 ProcessStartupInformation：Invoke-CimMethod 对它报
  //   "无法提供 .NET Framework 的 CimType"（实测失败）。
  //   控制台窗口靠"用 pythonw"解决，不靠启动参数。
  const ps =
    'Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{' +
    `CommandLine=${q(cmdLine)}; CurrentDirectory=${q(cwd)}` +
    '} | Out-Null'
  const psExe = join(
    process.env.SystemRoot || 'C:\\Windows',
    'System32',
    'WindowsPowerShell',
    'v1.0',
    'powershell.exe',
  )
  try {
    flog?.(`launch: ${cmdLine} (cwd=${cwd})`)
    // ★ 用 spawnSync 而不是 spawn：必须**确认 WMI 真的创建成功**。
    //   之前用 spawn 时，PowerShell 可能因参数/策略问题立即退出，
    //   而 spawn 本身不报错 → 被当成"已发起"，于是既不拉起也不 fallback，
    //   表现就是"DSH 不再拉起桌宠"。
    const r = spawnSync(
      psExe,
      ['-NoProfile', '-NonInteractive', '-Command', ps],
      { encoding: 'utf8', timeout: 20000, windowsHide: true },
    )
    const out = String(r.stdout || '')
    const errOut = String(r.stderr || '')
    flog?.(`powershell status=${r.status} stdout=${JSON.stringify(out)} stderr=${JSON.stringify(errOut)}`)
    if (r.error) {
      flog?.(`powershell spawn error: ${r.error.message}`)
      return false
    }
    if (r.status !== 0) return false
    // Invoke-CimMethod 输出表格，ReturnValue 0 表示成功
    const m = out.match(/ReturnValue\s+(\d+)/) || out.match(/(\d+)\s*$/m)
    if (m && Number(m[1]) !== 0) {
      flog?.(`WMI ReturnValue=${m[1]} (非 0 表示失败)`)
      return false
    }
    logger?.info?.(`[dsh-pet-pyside6] 已通过 WMI 拉起桌宠: ${cmdLine}`)
    return true
  } catch (error) {
    logger?.warn?.(`[dsh-pet-pyside6] WMI 启动器异常: ${error instanceof Error ? error.message : String(error)}`)
    return false
  }
}

export function apply(ctx, config = {}) {
  const logger = ctx.logger ?? console
  const port = Number(config.port) || 3080
  let child = null
  let disposed = false

  // 文件日志：DSH 的 logger 不好翻，出问题时靠它定位
  const flog = makeFileLog(config.projectDir || DEFAULT_PROJECT)
  flog(`apply() called, port=${port} autoStart=${config.autoStart} projectDir=${config.projectDir}`)

  // ---- 1) 启动（单实例：先探测再决定）----
  async function ensureRunning() {
    flog('ensureRunning() enter')
    if (disposed || config.autoStart === false) {
      flog(`ensureRunning() skip: disposed=${disposed} autoStart=${config.autoStart}`)
      return
    }
    if (await probe(port)) {
      flog(`ensureRunning() reuse: port ${port} already alive`)
      logger.info?.(`[dsh-pet-pyside6] 端口 ${port} 上已有实例，复用，不重复拉起`)
      return
    }
    flog(`ensureRunning() port ${port} not alive -> will launch`)
    const dir = config.projectDir || DEFAULT_PROJECT
    const main = join(dir, 'main.py')
    if (!existsSync(main)) {
      flog(`ensureRunning() abort: ${main} not found`)
      logger.warn?.(`[dsh-pet-pyside6] 找不到 ${main}，跳过启动`)
      return
    }
    // 空字符串 = 未显式配置，交给 launchViaWmi 决定（优先 pythonw，见那里）
    const python = config.pythonPath || ''
    try {
      // ★ 不能用 spawn(detached:true) 了。
      //   实测：DSH 的 host 进程处在一个 Job Object 里，且该 Job 带
      //   JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE —— DSH 退出时 Job 关闭，
      //   所有属于它的子进程都会被连带杀掉。
      //   Node 的 detached:true 只设 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP，
      //   **不会脱离 Job**；而 CREATE_BREAKAWAY_FROM_JOB 会被拒（WinError 5，
      //   Job 没开 BREAKAWAY_OK）。
      //   唯一可靠的办法是让**别的进程**去创建它：走 WMI 的 Win32_Process.Create，
      //   进程由 WmiPrvSE 创建，父进程不是 DSH，自然不在那个 Job 里。
      //   （实测：WMI 创建的子进程 IsProcessInJob = False）
      const ok = launchViaWmi(python, main, dir, logger, flog)
      flog(`launchViaWmi -> ${ok}`)
      if (!ok) {
        // WMI 不可用时退回直接 spawn（至少能跑，只是 DSH 退出会被带走）
        flog('fallback: detached spawn')
        logger.warn?.('[dsh-pet-pyside6] WMI 启动失败，退回 detached spawn（DSH 退出可能一并关闭）')
        const fbPy = python || 'python'
        child = spawn(fbPy, ['-u', main], {
          cwd: dir,
          detached: true,
          stdio: 'ignore',
          windowsHide: true,
        })
        child.unref()
        child.on('exit', (code) => {
          flog(`spawned child exit code=${code}`)
          logger.info?.(`[dsh-pet-pyside6] 桌宠进程退出 code=${code}`)
          child = null
        })
        child.on('error', (error) => {
          flog(`spawned child error: ${error.message}`)
          logger.warn?.(`[dsh-pet-pyside6] 启动失败: ${error.message}`)
          child = null
        })
        logger.info?.(`[dsh-pet-pyside6] 已拉起桌宠 pid=${child.pid}`)
      }
    } catch (error) {
      logger.warn?.(`[dsh-pet-pyside6] 启动异常: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  void ensureRunning()

  // ---- 2) 会话事件 → 桌宠 ----
  let offEvent
  if (config.forwardEvents !== false) {
    // 用根总线，跨 session 都能收到；抛错绝不能冒泡到共享总线
    const bus = ctx.root ?? ctx
    offEvent = bus.on?.('session/event', (session, event) => {
      try {
        const type = event?.type
        if (!type) return
        void push(port, { type, data: event.data ?? event.payload ?? {} }, logger)
      } catch (error) {
        logger.warn?.(`[dsh-pet-pyside6] 事件处理异常: ${error instanceof Error ? error.message : String(error)}`)
      }
    }, { global: true })
  }

  // ---- 3) 余额 → 桌宠 ----
  const query = createBalanceQuery(ctx, { version: '0.1.0' })
  const monitor = createBalanceMonitor({
    query,
    intervalMs: 15_000,   // 15 秒推一次：桌宠重启后最多等 15 秒就有余额
    onChange: (snapshot) => {
      void push(port, { type: 'balance', data: snapshot }, logger)
    },
  })
  if (config.balanceEnabled !== false) {
    monitor.configure(true, config.balanceSource || 'auto')
    // ★ DSH 重启后 credentials / settings 服务是异步就绪的，
    //   首次 configure 常拿到 NOT_CONFIGURED（表现为桌宠一直显示"余额未配置"）。
    //   这里做一轮重试：只要还是未配置，就每 10 秒强制重查，直到拿到真实结果。
    try {
      let attempts = 0
      const retry = setInterval(() => {
        attempts += 1
        try {
          const snap = monitor.snapshot?.()
          const st = snap?.status
          if (st === 'not-configured' || st === 'disabled' || st === 'loading') {
            if (attempts > 30) { clearInterval(retry); return }   // 最多重试 5 分钟
            monitor.configure(true, config.balanceSource || 'auto', true)
          } else {
            clearInterval(retry)
          }
        } catch {
          clearInterval(retry)
        }
      }, 10_000)
      retry.unref?.()
    } catch { /* ignore */ }
  }

  // ---- 4) 本地路由：/plugins/dsh-pet-pyside6/balance ----
  try {
    ctx.inject?.(['webServer'], (httpCtx) => {
      httpCtx.effect(
        () => httpCtx.webServer.register({ kind: 'exact', path: BALANCE_ENDPOINT, handler: createBalanceHandler(monitor) }),
        'dsh-pet-pyside6: local balance endpoint',
      )
    })
  } catch (error) {
    logger.warn?.(`[dsh-pet-pyside6] 注册余额路由失败: ${error instanceof Error ? error.message : String(error)}`)
  }

  // ---- 5) 清理 ----
  ctx.effect?.(() => () => {
    disposed = true
    try { offEvent?.() } catch { /* ignore */ }
    try { monitor.stop() } catch { /* ignore */ }
    // ★ 刻意**不杀**桌宠进程：它是 detached 的独立进程，DSH 退出/重启后它继续活着，
    //   下次插件启动靠端口探测复用它。要关桌宠走它自己的右键菜单「退出桌宠」。
    //   （早先这里 kill 会导致：DSH 重启 → 桌宠消失 → 再被重新拉起，边界条件差时就是"没了"）
  })
}

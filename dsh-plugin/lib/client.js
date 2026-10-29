window.__ModuleLoader__.load({ id: 'dsh-pet-pyside6', factory: (require) => {
  const module = { exports: {} }
  const exports = module.exports
  const React = require('react')
  const { useEffect, useState } = React
  const h = React.createElement

  const BALANCE_ENDPOINT = '/plugins/dsh-pet-pyside6/balance'

  function PetCard() {
    const [snapshot, setSnapshot] = useState(null)
    const [error, setError] = useState('')

    useEffect(() => {
      let live = true
      fetch(BALANCE_ENDPOINT, { cache: 'no-store' })
        .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
        .then((data) => { if (live) setSnapshot(data) })
        .catch((reason) => { if (live) setError(String(reason && reason.message ? reason.message : reason)) })
      return () => { live = false }
    }, [])

    const rows = [
      h('div', { key: 'title', style: { fontWeight: 600, marginBottom: 6 } }, 'PySide6 桌宠'),
      h('div', { key: 'desc', style: { opacity: 0.75 } },
        '独立可运行：桌宠已经在跑时，DSH 启动不会重复拉起；DSH 的事件与余额由本插件推送给它。'),
    ]

    if (error) {
      rows.push(h('div', { key: 'err', style: { marginTop: 8 } }, `余额接口：${error}`))
    } else if (!snapshot) {
      rows.push(h('div', { key: 'loading', style: { marginTop: 8, opacity: 0.6 } }, '余额加载中…'))
    } else if (snapshot.status === 'ready') {
      rows.push(h('div', { key: 'bal', style: { marginTop: 8 } }, snapshot.summary || '余额已就绪'))
    } else if (snapshot.status === 'not-configured' || snapshot.status === 'disabled') {
      rows.push(h('div', { key: 'none', style: { marginTop: 8, opacity: 0.7 } },
        snapshot.error || '未配置余额来源（可在 DSH 的模型/账户设置里配置后自动生效）'))
    } else {
      rows.push(h('div', { key: 'other', style: { marginTop: 8 } }, snapshot.error || snapshot.summary || '余额暂不可用'))
    }

    return h('div', { style: { fontSize: 13, lineHeight: 1.7 } }, rows)
  }

  function apply(ctx) {
    for (const slot of ['plugins.bundle.config', 'settings.plugins.tab', 'settings.plugin.item']) {
      try {
        ctx.slots.inject(slot, () => {
          try {
            ctx.slots.register({
              name: slot,
              key: 'dsh-pet-pyside6',
              id: 'dsh-pet-pyside6',
              order: 31,
              label: () => 'PySide6 桌宠',
              inject: () => ({}),
            }, PetCard)
          } catch (error) {
            if (typeof console !== 'undefined' && console.error) {
              console.error('[dsh-pet-pyside6] 注册设置卡片失败', error)
            }
          }
        })
      } catch (error) {
        if (typeof console !== 'undefined' && console.error) {
          console.error('[dsh-pet-pyside6] 注入设置 slot 失败', error)
        }
      }
    }
  }

  module.exports = {
    name: 'dsh-pet-pyside6-client',
    inject: ['slots'],
    apply,
  }
  return module.exports
} })

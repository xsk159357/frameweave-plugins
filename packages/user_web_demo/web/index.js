// S1-T7 示例插件：插件 web 目录注入脚本（EXTENSION_WEB_DIRS 等价，R-平台-7）
//
// 加载链路：
//   1) 平台注入机制（web/src/plugins.ts）调用 GET /api/extensions 拿到清单；
//   2) <script src="http://127.0.0.1:8788/extensions/user_web_demo/web/index.js"> 注入；
//   3) 本脚本执行 → window.fwExtensions.register({...}) 自我注册；
//   4) 加载器将 mount(host, ctx) 挂到画布 #fw-ext-root 的专属 slot，注入自定义组件。
//
// 契约：window.fwExtensions 由平台保证先于本脚本存在；sandbox/capabilities
// 由 manifest 声明，本脚本不要求额外权限。
(function () {
  'use strict'
  var PKG = 'user_web_demo'
  var TYPE_ID = 'user/web_demo'
  var VERSION = '1.0.0'

  // 相对资源：以本脚本 URL 为基准取同级资源（web/style.css），不依赖文档源。
  var scriptSrc = (document.currentScript && document.currentScript.src) || ''
  var base = scriptSrc.slice(0, scriptSrc.lastIndexOf('/') + 1)
  var link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = base + 'style.css'
  document.head.appendChild(link)

  function mount(host) {
    var card = document.createElement('div')
    card.className = 'fw-ext-panel fw-ext-demo'
    card.setAttribute('data-fw-ext-demo', '1')
    card.setAttribute('data-count', '0')
    card.innerHTML =
      '<div class="fw-ext-demo-title">🎨 Web 扩展组件</div>' +
      '<div class="fw-ext-demo-sub">' + TYPE_ID + ' · 画布注入成功</div>' +
      '<div class="fw-ext-demo-row">' +
      '<button class="fw-ext-demo-btn" type="button">点我 +1</button>' +
      '<span class="fw-ext-demo-count">0</span>' +
      '</div>'
    var count = 0
    var btn = card.querySelector('.fw-ext-demo-btn')
    var num = card.querySelector('.fw-ext-demo-count')
    btn.addEventListener('click', function () {
      count += 1
      num.textContent = String(count)
      card.setAttribute('data-count', String(count))
    })
    host.appendChild(card)
  }

  function unmount(host) {
    host.innerHTML = ''
  }

  if (window.fwExtensions && typeof window.fwExtensions.register === 'function') {
    window.fwExtensions.register({
      pkg: PKG,
      type_id: TYPE_ID,
      version: VERSION,
      mount: mount,
      unmount: unmount,
    })
  } else {
    // 平台桥缺失（理论不可达：加载器保证先建桥）——保留元素级提示便于排查
    console.error('[web-ext:' + PKG + '] window.fwExtensions 不存在，注入跳过')
  }
})()

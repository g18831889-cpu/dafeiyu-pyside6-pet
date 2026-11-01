"""src/host/server.py - 双模通信服务端（HTTP / stdio）"""
import sys
import socket
import threading
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

class StateStore:
    # 记忆模式的"租约"时长（秒）。
    #
    # ★ 为什么是租约而不是开关：灵魂（DSH 插件）可能崩溃、被卸载、或 DSH 直接退出，
    #   而桌宠是**独立进程**，它不知道对方已经没了。若只用一个布尔开关，
    #   灵魂一旦掉线，桌宠就永远停在记忆模式里 —— 原生碎碎念已停、又没人说话，
    #   表现是"桌宠变哑巴"。租约到期自动交还，最坏情况只是哑一会儿。
    MEMORY_LEASE_SEC = 90

    def __init__(self):
        self.lock = threading.Lock()
        self.counter = 0
        self.work_status = None 
        # 余额快照：由 DSH 插件（dsh-pet-pyside6）推过来的 type='balance' 事件写入。
        # 独立模式下没人推 → 保持 None，菜单会走"未连接"分支而不是伪造数字。
        self.balance = None
        # ---- 记忆外挂（可选功能，与核心完全解耦）----
        # 「灵魂」插件通过 POST /say 推来 {anim, text, duration}，这里只存快照。
        # say_counter 每次递增，window.poll_state 靠它判断"有新指令"。
        # 没人推 → 永远 0，桌宠行为与没有本功能时完全一致。
        self.say_data = None
        self.say_counter = 0
        # 记忆模式：开启后原生 _fire_whisper 定时器让位给灵魂驱动。
        # 存的是**到期时间戳**（0 = 未开启），不是布尔值。
        self.memory_until = 0.0
        # 「摸摸头」计数：单击桌宠时 +1，单调递增，永不重置。
        #
        # ★ 为什么是计数而不是事件：桌宠不能反向推事件给灵魂（/event 是
        #   DSH→身体 单向通道），而灵魂本来就每 20 秒读一次 GET /state。
        #   让它按**差值**结算摸头次数，就不用新开端口、不用改协议。
        #   没人单击 → 永远 0，与没有本功能时完全一致。
        self.pet_counter = 0
        # 对话端口：灵魂接管时把它自己的 /chat 端口告诉我们（POST /memory-mode 的 chatPort）。
        # 点「技能 ▸ 对话」时才知道往哪儿发。0 = 还没连上，菜单项置灰。
        self.chat_port = 0
        # 对话结果：/chat 在后台线程里等模型，回来塞这里，window.poll_state 取走弹气泡。
        # ★ 为什么不在 /chat 处理器里直接弹：那会在 HTTP 线程里操作 Qt，必崩。
        self.chat_result = None
        self.chat_counter = 0
        # 对话进行中标记（防止连点两次弹两个输入框）
        self.chat_busy = False

    def set_status(self, status_idx):
        with self.lock:
            if self.work_status != status_idx:
                self.work_status = status_idx
                self.counter += 1

    def set_balance(self, snapshot):
        with self.lock:
            self.balance = snapshot

    def get_balance(self):
        with self.lock:
            return self.balance
                
    def get_state(self):
        with self.lock:
            return {
                "counter": self.counter,
                "workStatus": self.work_status,
                # 灵魂每 20 秒读一次，按差值结算摸头
                "petCounter": self.pet_counter,
            }

    def bump_pet(self):
        """单击桌宠 → 摸头 +1。只记账，不改任何原生行为。"""
        with self.lock:
            self.pet_counter += 1
            return self.pet_counter

    # ---- 记忆外挂相关（可选；不调用则零影响）----
    def set_say(self, payload):
        """灵魂推来一条「动作 + 话」。只存快照，由 poll_state 消费。"""
        with self.lock:
            self.say_data = payload
            self.say_counter += 1
            # 说话即视为灵魂活着，顺手续租：正常对话期间不用额外心跳
            if self.memory_until:
                self.memory_until = time.time() + self.MEMORY_LEASE_SEC

    def take_say(self):
        """取走待播指令（取完即清，避免同一句重复播）。

        返回 (payload, counter)；payload 为 None 表示没有新指令。
        """
        with self.lock:
            if self.say_data is None:
                return None, self.say_counter
            payload = self.say_data
            self.say_data = None
            return payload, self.say_counter

    def set_memory_mode(self, on, lease_sec=None, chat_port=None):
        """开启/关闭记忆模式。开启时带租约，到期自动失效。

        chat_port：灵魂的 /chat 端口。接管时顺手告诉我们，省一次握手。
        """
        with self.lock:
            if chat_port is not None:
                try:
                    cp = int(chat_port)
                    if cp > 0:
                        self.chat_port = cp
                except (TypeError, ValueError):
                    pass
            if not on:
                self.memory_until = 0.0
                return
            sec = self.MEMORY_LEASE_SEC if lease_sec is None else max(1, int(lease_sec))
            self.memory_until = time.time() + sec

    def renew_memory_lease(self, lease_sec=None):
        """续租（灵魂心跳用）。未开启时不隐式开启，返回 False。"""
        with self.lock:
            if not self.memory_until:
                return False
            sec = self.MEMORY_LEASE_SEC if lease_sec is None else max(1, int(lease_sec))
            self.memory_until = time.time() + sec
            return True

    def get_memory_mode(self):
        """当前是否处于记忆模式（已考虑租约过期）。"""
        with self.lock:
            if not self.memory_until:
                return False
            if time.time() > self.memory_until:
                self.memory_until = 0.0   # 惰性过期
                return False
            return True

    def memory_lease_left(self):
        """剩余租约秒数（未开启返回 0）。"""
        with self.lock:
            if not self.memory_until:
                return 0
            return max(0, int(self.memory_until - time.time()))

    # ---- 对话（可选；没人点菜单就零影响）----
    def set_chat_result(self, payload):
        """后台线程把模型回复塞进来。只存快照，由 poll_state 消费。"""
        with self.lock:
            self.chat_result = payload
            self.chat_counter += 1
            self.chat_busy = False

    def take_chat_result(self):
        """取走对话结果（取完即清）。返回 (payload, counter)。"""
        with self.lock:
            if self.chat_result is None:
                return None, self.chat_counter
            payload = self.chat_result
            self.chat_result = None
            return payload, self.chat_counter

    def get_chat_port(self):
        with self.lock:
            return self.chat_port

    def begin_chat(self):
        """标记对话开始。已在对话中返回 False（防止连点弹两个输入框）。"""
        with self.lock:
            if self.chat_busy:
                return False
            self.chat_busy = True
            return True

    def is_chat_busy(self):
        with self.lock:
            return self.chat_busy

store = StateStore()

def process_event(data):
    """解析 DSH 事件，更新状态"""
    event_type = data.get('type')
    # 调试：记录每个事件（排查状态卡住用）
    try:
        from src.host.logging_setup import get_logger
        _lg = get_logger()
        if _lg.handlers:
            _lg.info('DSH事件: %s  data=%s', event_type, str(data.get('data'))[:120])
    except Exception:
        pass
    status_idx = None
    
    if event_type == 'turn/start':
        status_idx = 0
    elif event_type == 'tool/call':
        tool_name = data.get('data', {}).get('name', '')
        status_idx = 3 if tool_name == 'ask_user_question' else 1
    elif event_type == 'tool/result':
        status_idx = 2
    elif event_type == 'approval/asked':
        status_idx = 3
    elif event_type == 'turn/end':
        reason = data.get('data', {}).get('reason', {}).get('kind', '')
        if reason == 'completed':
            status_idx = 4      # 雀跃庆祝
        elif reason in ('error', 'max-tokens', 'timeout', 'failed'):
            status_idx = 5      # 垂头叹气
        elif reason == 'blocked':
            status_idx = 3      # 等待确认
        else:
            # ★ 其它任何结束原因（cancelled / interrupted / aborted / 空 ...）
            #   都必须回空闲，否则状态永远卡在上一次的值，
            #   桌宠就一直循环工作动画、不进随机链（表现：站着不动）
            status_idx = 'IDLE'
        
    if status_idx == 'IDLE':
        store.set_status(None)          # 回空闲
    elif status_idx is not None:
        store.set_status(status_idx)

    # 余额：DSH 插件 dsh-pet-pyside6 定期推送，直接存快照（渲染在菜单里做）
    if event_type == 'balance':
        try:
            from src.host.logging_setup import get_logger
            _snap = data.get('data') if isinstance(data.get('data'), dict) else data
            get_logger().info('收到 balance: status=%s summary=%s error=%s',
                             (_snap or {}).get('status'), (_snap or {}).get('summary'),
                             (_snap or {}).get('error'))
        except Exception:
            pass
        store.set_balance(data.get('data') if isinstance(data.get('data'), dict) else data)

def spawn_chat(store, port, text):
    """后台线程请求灵魂的 /chat，结果塞进 store。

    抽成模块级函数是为了让 window.py（右键菜单「对话」）也能直接用，
    不必绕一圈 HTTP 打自己。

    ★ 三条纪律：
      1. 绝不阻塞调用方线程（模型要 1~3 秒）
      2. 失败也要给结果（气泡显示"我有点懵"），否则主人以为她没听见
      3. 异常一律吞掉——对话坏了不该拖垮桌宠主进程
    """
    def worker():
        try:
            payload = json.dumps({'text': text}).encode('utf-8')
            req = urllib.request.Request(
                f'http://127.0.0.1:{port}/plugins/dsh-pet-memory/chat',
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST')
            # 模型最多 30 秒，这里给 35 秒余量
            with urllib.request.urlopen(req, timeout=35) as resp:
                raw = resp.read().decode('utf-8', 'replace')
            try:
                out = json.loads(raw)
            except Exception:
                out = {'ok': False, 'text': '我有点懵…'}
            store.set_chat_result({
                'ok': bool(out.get('ok')),
                'text': str(out.get('text') or ''),
                'anim': str(out.get('anim') or ''),
            })
        except Exception as e:
            try:
                store.set_chat_result({
                    'ok': False,
                    'text': '我有点懵…再说一次？',
                    'anim': '',
                    'error': str(e),
                })
            except Exception:
                pass

    t = threading.Thread(target=worker, daemon=True)
    t.start()


class DSHHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def _spawn_chat(self, port, text):
        spawn_chat(store, port, text)

    def do_GET(self):
        if self.path == '/balance':
            # 调试：直接看 store 里最新的余额快照
            snap = None
            try:
                snap = store.get_balance()
            except Exception:
                pass
            body = json.dumps({'balance': snap}, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/pets':
            # 调试：当前存活宠物数量与位置
            try:
                from src.runtime.window import ALL_PETS
                rows = []
                for p in ALL_PETS:
                    try:
                        if p.isVisible():
                            rows.append({"label": getattr(p, 'pet_label', ''),
                                         "main": bool(getattr(p, 'is_main', False)),
                                         "x": p.x(), "y": p.y(),
                                         "scale": round(float(getattr(p, 'scale', 0)), 2)})
                    except Exception:
                        pass
            except Exception:
                rows = []
            body = json.dumps({"count": len(rows), "pets": rows}, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/state':
            data = store.get_state()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))
        elif self.path == '/say':
            # 调试用：看当前待播指令与记忆模式状态（不消费，不影响播放）
            body = json.dumps({'pending': store.say_data,
                               'counter': store.say_counter,
                               'memoryMode': store.get_memory_mode()},
                              ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
            
    def do_POST(self):
        if self.path == '/idle':
            # 手动解锁：把工作状态清空，桌宠立刻恢复随机动作链
            store.set_status(None)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # ---- 记忆外挂（可选）：/say 与 /memory-mode ----
        # 记忆外挂删掉后这两个端点无人调用，桌宠行为与原来完全一致。
        if self.path == '/say':
            length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                anim = data.get('anim') or ''
                text = data.get('text') or ''
                try:
                    duration = int(data.get('duration') or 12000)
                except Exception:
                    duration = 12000
                store.set_say({'anim': anim, 'text': text, 'duration': duration})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
            return
        if self.path == '/memory-mode':
            length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                lease = data.get('leaseSec')
                store.set_memory_mode(data.get('on'), lease, data.get('chatPort'))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(
                    {'ok': True,
                     'memoryMode': store.get_memory_mode(),
                     'leaseLeft': store.memory_lease_left(),
                     'chatPort': store.get_chat_port()}).encode('utf-8'))
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
            return
        # 对话：把主人这句话转给灵魂（DSH 插件的 /chat），结果异步塞进 store。
        # ★ 必须在后台线程里发请求：模型要 1~3 秒，阻塞 HTTP 线程会让
        #   poll_state 拿不到数据、整个桌宠卡住。
        if self.path == '/chat':
            length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                text = (data.get('text') or '').strip()
                if not text:
                    raise ValueError('empty text')
                port = store.get_chat_port()
                if not port:
                    body = json.dumps({'ok': False, 'error': 'chat port not set'}).encode('utf-8')
                    self.send_response(409)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                # 立刻回 202：告诉调用方"收到了，等气泡"
                body = json.dumps({'ok': True, 'accepted': True}).encode('utf-8')
                self.send_response(202)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self._spawn_chat(port, text)
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
            return
        # 续租：灵魂心跳调用。未开启时不隐式开启（避免"幽灵接管"）。
        if self.path == '/memory-renew':
            length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(length)
            try:
                data = json.loads(post_data.decode('utf-8')) if post_data else {}
            except Exception:
                data = {}
            ok = store.renew_memory_lease(data.get('leaseSec'))
            body = json.dumps({'ok': ok,
                               'memoryMode': store.get_memory_mode(),
                               'leaseLeft': store.memory_lease_left()}).encode('utf-8')
            self.send_response(200 if ok else 409)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/pet':
            # 摸头 +1。正常路径是 window.py 单击时直接调 store.bump_pet()，
            # 这个端点只为调试/外部脚本手动触发用（不改变任何原生行为）。
            store.bump_pet()
            body = json.dumps({'ok': True, 'petCounter': store.get_state()['petCounter']}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/event':
            length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                process_event(data)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def probe_instance(port=3080, timeout=0.6):
    """已有实例在跑？（GET /state 能应答即算）"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


class ExclusiveHTTPServer(HTTPServer):
    """独占端口的 HTTPServer —— 端口已被别的进程占用时**绑定失败**。

    ★ 为什么必须自己写这个类：
      标准库 `HTTPServer.allow_reuse_address` 默认是 True（Windows 上等价于
      SO_REUSEADDR）。Windows 的 SO_REUSEADDR 语义与 Linux 完全不同 —— 它允许
      **另一个进程直接绑上一个正在被监听的端口**，且不报错。于是：
        · 第二只桌宠能绑上 3080，`start_http_server` 返回 True，单实例闸形同虚设；
        · 更糟的是连接仍然全部路由给**先绑的那个**（实测 10/10），
          第二只鱼既收不到 DSH 事件、又抢不到端口，变成一只"空壳鱼"。
      所以这里做两件事：
        1. `allow_reuse_address = False`（不再主动设置 SO_REUSEADDR）；
        2. 绑之前先设 `SO_EXCLUSIVEADDRUSE`（Windows 专有，语义是"只有我能用"），
           这样端口被占时 bind 会抛 WinError 10048，调用方立刻知道"已有实例"。
      `SO_EXCLUSIVEADDRUSE` 不会被 TIME_WAIT 挡住（已实测：关闭后立即独占重绑成功），
      所以不影响桌宠正常退出后马上重启。
    """
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


def start_http_server(port=3080):
    try:
        server = ExclusiveHTTPServer(('127.0.0.1', port), DSHHandler)
    except OSError as e:
        # 端口被占：几乎一定是另一个实例已经在跑。进程自己不要崩，交给调用方决定。
        print(f"[Server] 端口 {port} 已被占用（可能已有实例在运行）：{e}", file=sys.stderr, flush=True)
        return False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[Server] HTTP Server started on 127.0.0.1:{port}", flush=True)
    return True

def start_stdio_server(proxy_to=None):
    """作为 DSH 插件时，监听标准输入（支持 MCP 协议）。

    proxy_to 不为 None 时进入**代理模式**：不建窗口、不做断尾求生，
    只把 DSH 推来的事件转给已经在运行的那个实例（默认 3080 上的独立实例）。
    这样 DSH 启动时不会重复拉起一只桌宠。
    """
    def _dispatch(ev):
        if not proxy_to:
            return process_event(ev)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{proxy_to}/event",
                data=json.dumps(ev, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1.5).read()
        except Exception as e:
            print(f"[Server] 事件转发失败（{proxy_to}）：{e}", file=sys.stderr, flush=True)
    def send_response(resp):
        sys.stdout.write(json.dumps(resp) + '\n')
        sys.stdout.flush()

    def _listen():
        print("[Server] Stdio MCP Server started.", file=sys.stderr, flush=True)
        for line in sys.stdin:
            text = line.strip()
            if not text: continue
            try:
                msg = json.loads(text)
                method = msg.get('method')
                msg_id = msg.get('id')
                
                if method == 'initialize':
                    send_response({
                        "jsonrpc": "2.0", "id": msg_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"resources": {}, "tools": {}},
                            "serverInfo": {"name": "pyside6-pet", "version": "1.0.1"}
                        }
                    })
                elif method == 'notifications/initialized':
                    pass
                elif method == 'tools/list':
                    send_response({
                        "jsonrpc": "2.0", "id": msg_id, 
                        "result": {
                            "tools": [{
                                "name": "dsh_event",
                                "description": "接收来自 DSH 宿主的工作状态事件",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string"},
                                        "data": {"type": "object"}
                                    }
                                }
                            }]
                        }
                    })
                elif method == 'tools/call':
                    params = msg.get('params', {})
                    if params.get('name') == 'dsh_event':
                        event_data = params.get('arguments', {})
                        _dispatch(event_data)
                        send_response({
                            "jsonrpc": "2.0", "id": msg_id,
                            "result": {"content": [{"type": "text", "text": "Event processed"}]}
                        })
                elif method == 'resources/list':
                    send_response({"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}})
                else:
                    # 兜底：如果是自定义的 DSH 事件
                    _dispatch(msg)
                    if msg_id is not None:
                        send_response({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            except Exception as e:
                print(f"[Server] Error parsing stdio: {e}", file=sys.stderr, flush=True)
        
        # 当 stdin 被关闭（DSH 退出），自动触发重生为独立进程
        if proxy_to:
            # 代理模式：桌宠在别处跑着，DSH 退出就干净退出，不要再起一只
            print("[Server] 代理模式：DSH 断开，直接退出", file=sys.stderr, flush=True)
            os._exit(0)
        print("[Server] DSH 断开连接，正在转为独立运行模式...", file=sys.stderr, flush=True)
        import subprocess
        import os
        DETACHED_PROCESS = 0x00000008
        # ★ cwd 必须显式给：DSH 可能从任意目录拉起本进程，相对路径 main.py 会找不到
        _proj = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        subprocess.Popen(
            [sys.executable, "-u", "main.py"],
            cwd=_proj,
            creationflags=DETACHED_PROCESS,
            close_fds=True
        )
        os._exit(0) # 退出当前进程
                
    t = threading.Thread(target=_listen, daemon=True)
    t.start()
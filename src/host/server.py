"""src/host/server.py - 双模通信服务端（HTTP / stdio）"""
import sys
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
            return {"counter": self.counter, "workStatus": self.work_status}

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

    def set_memory_mode(self, on, lease_sec=None):
        """开启/关闭记忆模式。开启时带租约，到期自动失效。"""
        with self.lock:
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

class DSHHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
        
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
                store.set_memory_mode(data.get('on'), lease)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(
                    {'ok': True,
                     'memoryMode': store.get_memory_mode(),
                     'leaseLeft': store.memory_lease_left()}).encode('utf-8'))
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


def start_http_server(port=3080):
    try:
        server = HTTPServer(('127.0.0.1', port), DSHHandler)
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
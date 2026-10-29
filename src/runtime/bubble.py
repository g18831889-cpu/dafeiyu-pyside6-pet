"""src/runtime/bubble.py - 悬浮气泡（白底）"""
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QFont, QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

class BubbleWindow(QWidget):
    def __init__(self, parent=None):
        # ★ 置顶标志跟随 window.py 里的全局「置顶」开关：
        #   用户关掉置顶后，新创建的气泡也不该压在最上层。
        _flags = (Qt.FramelessWindowHint | Qt.Tool
                  | Qt.WindowDoesNotAcceptFocus | Qt.BypassWindowManagerHint)
        try:
            from src.runtime.window import topmost_enabled
            if topmost_enabled():
                _flags |= Qt.WindowStaysOnTopHint
        except Exception:
            _flags |= Qt.WindowStaysOnTopHint      # 拿不到就用原来的默认行为
        super().__init__(None, _flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        
        self.parent_pet = parent
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(4)
        
        self.title_label = QLabel("标题")
        self.title_label.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        self.title_label.setStyleSheet("color: #333333; background: transparent;")
        
        self.content_label = QLabel("内容")
        self.content_label.setFont(QFont("Microsoft YaHei", 9))
        self.content_label.setStyleSheet("color: #555555; background: transparent;")
        self.content_label.setWordWrap(True)
        
        layout.addWidget(self.title_label)
        layout.addWidget(self.content_label)
        
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        
        self.hide()

    def mouseDoubleClickEvent(self, event):
        """双击气泡 → 把桌宠刷到最上层。

        气泡是独立顶层窗口，被遮住时用户往往先点到它；
        双击让它把主人一起顶上来。
        """
        try:
            if self.parent_pet is not None and hasattr(self.parent_pet, 'bring_to_front'):
                self.parent_pet.bring_to_front()
        except Exception:
            pass
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # 绘制圆角白色背景，灰色边框
        bg_color = QColor(255, 255, 255, 240)
        border_color = QColor(200, 200, 200)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        
        painter.setBrush(bg_color)
        painter.setPen(QPen(border_color, 1))
        painter.drawRoundedRect(rect, 10, 10)
        painter.end()

    def show_message(self, title, content, duration_ms=12000):
        self.title_label.setText(title)
        self.content_label.setText(content)
        
        self.content_label.setFixedWidth(220)
        self.adjustSize()
        
        self.update_position()
        self.show()
        # ★ 不要 raise_()：气泡是独立的 TOPMOST 顶层窗口，raise_() 会把它
        #   连带整组置顶窗口提到前台，导致"工作时桌宠自己跳到别的应用上层"。
        #   它本来就带 WindowStaysOnTopHint，show() 之后自然在最上层，
        #   不需要 raise_() 也能被看见。
        
        if duration_ms > 0:
            self.timer.start(duration_ms)

    def update_position(self):
        if not self.parent_pet: return
        pw = self.parent_pet.width()
        px, py = self.parent_pet.x(), self.parent_pet.y()
        bw = self.width()
        bh = self.height()
        
        x = px + (pw - bw) // 2
        y = py - bh - 10
        self.move(x, y)

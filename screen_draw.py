# -*- coding: utf-8 -*-
"""
屏幕画图工具（单文件，扁平画布，无图层）

功能一览
--------
- 右 Ctrl：进入 / 退出标注界面；退出默认丢弃本次未缓存的修改
- 1 画笔 / 2 橡皮 / 3 高亮（框选区域，区域外 70% 不透明灰遮罩）
  再次按同一个键回到选区模式
- 默认选区模式：拖拽框选笔画 / 文字 / 图片，可整体拖动；
  选区上方的圆形手柄用于旋转
- A 全选 · X 删除选中 · Z 撤销 · V 粘贴（文字 / 图片，文件忽略）
- C 保存到缓存并退出（缓存仅本次运行有效）
- 滚轮调节笔粗；底部工具栏可点选颜色与笔粗
- 系统托盘图标与菜单

依赖：PySide6、pynput
"""

from __future__ import annotations

import math
import queue
import sys
import time

# ---------------------------------------------------------------------------
# 依赖自检：缺什么直接给出安装命令，不做静默失败
# ---------------------------------------------------------------------------


def ensure_dependencies() -> None:
    missing = []
    for module, package in (("PySide6", "PySide6"), ("pynput", "pynput")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        print("缺少依赖: " + "、".join(missing))
        print("安装命令: {} -m pip install {}".format(sys.executable, " ".join(missing)))
        sys.exit(1)


ensure_dependencies()

from pynput import keyboard as pynput_keyboard  # noqa: E402
from PySide6.QtCore import (  # noqa: E402
    QObject, QPoint, QPointF, QRect, QRectF, QSizeF, QTimer, Qt,
)
from PySide6.QtGui import (  # noqa: E402
    QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QIcon, QPainter,
    QPainterPath, QPen, QPixmap, QTransform,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMenu, QSystemTrayIcon, QWidget,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PALETTE = [
    "#ff3b30", "#ff9500", "#ffd60a", "#34c759",
    "#0a84ff", "#bf5af2", "#ffffff", "#111111",
]
DIM_COLOR = QColor(70, 70, 70, 178)       # 灰色遮罩：alpha=178 ≈ 70% 不透明
SELECT_COLOR = QColor(10, 132, 255)
HINT_COLOR = QColor(255, 255, 255, 230)
DEFAULT_FONT_SIZE = 30                    # 粘贴文字的默认像素大小
ERASER_MIN_WIDTH = 12.0
UNDO_LIMIT = 50


def make_font(pixel_size: int) -> QFont:
    font = QFont("Microsoft YaHei")
    font.setPixelSize(pixel_size)
    return font


def rotate_point(point: QPointF, center: QPointF, degrees: float) -> QPointF:
    """把 point 绕 center 旋转 degrees 度。"""
    rad = math.radians(degrees)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    dx, dy = point.x() - center.x(), point.y() - center.y()
    return QPointF(
        center.x() + dx * cos_a - dy * sin_a,
        center.y() + dx * sin_a + dy * cos_a,
    )


def rotated_bbox(pos: QPointF, local: QRectF, angle: float) -> QRectF:
    """旋转对象（以自身中心为轴）后的轴对齐包围盒。"""
    rect = local.translated(pos)
    if abs(angle) < 1e-6:
        return rect
    center = rect.center()
    transform = (
        QTransform()
        .translate(center.x(), center.y())
        .rotate(angle)
        .translate(-center.x(), -center.y())
    )
    return transform.mapRect(rect)


def text_local_rect(shape: "Shape") -> QRectF:
    """文字对象未旋转时、左上角为原点的局部包围盒。"""
    font = make_font(shape.font_size)
    metrics = QFontMetricsF(font)
    lines = shape.text.split("\n") if shape.text else [""]
    width = max(metrics.horizontalAdvance(line) for line in lines)
    height = metrics.lineSpacing() * len(lines)
    return QRectF(0, 0, width, height)


def stroke_path(points: list[QPointF]) -> QPainterPath:
    """把采样点连成平滑路径（相邻点中点做二次贝塞尔）。"""
    path = QPainterPath(points[0])
    if len(points) < 3:
        path.lineTo(points[-1])
        return path
    for i in range(1, len(points) - 1):
        mid = QPointF(
            (points[i].x() + points[i + 1].x()) / 2,
            (points[i].y() + points[i + 1].y()) / 2,
        )
        path.quadTo(points[i], mid)
    path.lineTo(points[-1])
    return path


# ---------------------------------------------------------------------------
# 画布对象（扁平结构：按列表顺序依次绘制，没有图层概念）
# ---------------------------------------------------------------------------


class Shape:
    """kind: stroke / eraser / highlight / text / image"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.points: list[QPointF] = []
        self.color = QColor(PALETTE[0])
        self.width = 4.0
        self.rect = QRectF()          # highlight 专用
        self.text = ""
        self.font_size = DEFAULT_FONT_SIZE
        self.pixmap: QPixmap | None = None
        self.image_size = QSizeF()
        self.pos = QPointF()          # text / image 的左上角
        self.angle = 0.0              # text / image 的旋转角

    def copy(self) -> "Shape":
        shape = Shape(self.kind)
        shape.points = [QPointF(p) for p in self.points]
        shape.color = QColor(self.color)
        shape.width = self.width
        shape.rect = QRectF(self.rect)
        shape.text = self.text
        shape.font_size = self.font_size
        shape.pixmap = self.pixmap      # QPixmap 不可变共享，不深拷贝
        shape.image_size = QSizeF(self.image_size)
        shape.pos = QPointF(self.pos)
        shape.angle = self.angle
        return shape

    def local_rect(self) -> QRectF:
        if self.kind == "text":
            return text_local_rect(self)
        if self.kind == "image":
            return QRectF(0, 0, self.image_size.width(), self.image_size.height())
        return QRectF()

    def bbox(self) -> QRectF:
        if self.kind in ("stroke", "eraser"):
            if not self.points:
                return QRectF()
            xs = [p.x() for p in self.points]
            ys = [p.y() for p in self.points]
            pad = self.width / 2 + 1
            return QRectF(
                min(xs) - pad, min(ys) - pad,
                max(xs) - min(xs) + pad * 2, max(ys) - min(ys) + pad * 2,
            )
        if self.kind == "highlight":
            return QRectF(self.rect).normalized()
        if self.kind in ("text", "image"):
            return rotated_bbox(self.pos, self.local_rect(), self.angle)
        return QRectF()

    def translate(self, dx: float, dy: float) -> None:
        if self.kind == "highlight":
            self.rect.translate(dx, dy)
        elif self.kind in ("stroke", "eraser"):
            self.points = [QPointF(p.x() + dx, p.y() + dy) for p in self.points]
        else:
            self.pos = QPointF(self.pos.x() + dx, self.pos.y() + dy)

    def rotate_about(self, center: QPointF, degrees: float) -> None:
        """高亮是矩形遮罩，不参与旋转。"""
        if self.kind == "highlight":
            return
        if self.kind in ("stroke", "eraser"):
            self.points = [rotate_point(p, center, degrees) for p in self.points]
        else:
            local = self.local_rect()
            current_center = self.pos + local.center()
            new_center = rotate_point(current_center, center, degrees)
            self.pos = new_center - local.center()
            self.angle += degrees


# ---------------------------------------------------------------------------
# 标注层窗口
# ---------------------------------------------------------------------------


class Overlay(QWidget):
    def __init__(self) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("屏幕画图")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.ArrowCursor)
        self.setMouseTracking(True)

        self.bg = QPixmap()
        self.ink = QPixmap()
        self.shapes: list[Shape] = []
        self.cached: list[Shape] = []       # C 键写入的缓存（仅本次运行）
        self.undo_stack: list[list[Shape]] = []

        self.tool = "select"                # select / pen / eraser / highlight
        self.pen_color = QColor(PALETTE[0])
        self.pen_width = 4.0

        self.selection: list[Shape] = []
        self.live: Shape | None = None      # 正在画的笔迹（预览）
        self.marquee: QRectF | None = None  # 正在拖的框
        self.marquee_tool = "select"

        self.press_pos = QPointF()
        self.last_pos = QPointF()
        self.drag_mode: str | None = None
        self.drag_dirty = False             # 本次拖拽是否已产生变化（决定何时记撤销）
        self.rot_center = QPointF()
        self.rot_last = 0.0

        self.active = False

    # ------------------------------------------------------------------ 开关

    def open_overlay(self) -> None:
        if self.active:
            return
        self.shapes = [s.copy() for s in self.cached]
        self.undo_stack.clear()
        self.selection.clear()
        self.live = None
        self.marquee = None
        self.tool = "select"
        self.setCursor(Qt.ArrowCursor)
        self._capture_screens()             # 先截图，再显示窗口
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self.active = True
        self.render_ink()

    def close_overlay(self, save: bool) -> None:
        if not self.active:
            return
        if save:
            self.cached = [s.copy() for s in self.shapes]
        self.active = False
        self.live = None
        self.marquee = None
        self.selection.clear()
        self.shapes = []
        self.undo_stack.clear()
        self.hide()

    # ------------------------------------------------------------------ 截图

    def _capture_screens(self) -> None:
        screens = QGuiApplication.screens()
        virtual = QRect()
        for screen in screens:
            virtual = virtual.united(screen.geometry())
        dpr = max((screen.devicePixelRatio() for screen in screens), default=1.0)

        width = max(1, int(round(virtual.width() * dpr)))
        height = max(1, int(round(virtual.height() * dpr)))

        bg = QPixmap(width, height)
        bg.setDevicePixelRatio(dpr)
        bg.fill(Qt.black)
        painter = QPainter(bg)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for screen in screens:
            grab = screen.grabWindow(0)
            if grab.isNull():
                continue
            origin = QPointF(
                screen.geometry().x() - virtual.x(),
                screen.geometry().y() - virtual.y(),
            )
            painter.drawPixmap(origin, grab)
        painter.end()

        ink = QPixmap(width, height)
        ink.setDevicePixelRatio(dpr)
        ink.fill(Qt.transparent)

        self.bg = bg
        self.ink = ink
        self.setGeometry(virtual)

    # ------------------------------------------------------------------ 绘制

    def render_ink(self) -> None:
        if self.ink.isNull():
            return
        self.ink.fill(Qt.transparent)
        painter = QPainter(self.ink)
        painter.setRenderHint(QPainter.Antialiasing, True)
        for shape in self.shapes:
            self.draw_shape(painter, shape)
        painter.end()
        self.update()

    def draw_shape(self, painter: QPainter, shape: Shape) -> None:
        # 每个对象都从干净的 SourceOver 开始：橡皮会把合成模式留在 Clear，
        # 不重置的话它后面的文字/图片会以 Clear 绘制，看起来就是“透明看不见”。
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if shape.kind == "highlight":
            self._draw_highlight(painter, shape.rect)
        elif shape.kind in ("stroke", "eraser"):
            self._draw_stroke(painter, shape)
        elif shape.kind == "text":
            self._draw_text(painter, shape)
        elif shape.kind == "image":
            self._draw_image(painter, shape)

    def _draw_stroke(self, painter: QPainter, shape: Shape) -> None:
        erasing = shape.kind == "eraser"
        painter.setCompositionMode(
            QPainter.CompositionMode_Clear if erasing
            else QPainter.CompositionMode_SourceOver
        )
        color = QColor(0, 0, 0) if erasing else shape.color
        pen = QPen(color, shape.width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        if len(shape.points) == 1:
            painter.setBrush(color)
            painter.drawEllipse(shape.points[0], shape.width / 2, shape.width / 2)
            painter.setBrush(Qt.NoBrush)
        else:
            painter.drawPath(stroke_path(shape.points))

    def _draw_highlight(self, painter: QPainter, rect: QRectF) -> None:
        """框内保持原样，框外铺 70% 不透明灰。"""
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.setPen(Qt.NoPen)
        painter.setBrush(DIM_COLOR)
        area = QRectF(0, 0, self.width(), self.height())
        r = QRectF(rect).normalized()
        bands = [
            QRectF(0, 0, self.width(), r.top()),
            QRectF(0, r.bottom(), self.width(), self.height() - r.bottom()),
            QRectF(0, r.top(), r.left(), r.height()),
            QRectF(r.right(), r.top(), self.width() - r.right(), r.height()),
        ]
        for band in bands:
            band = band.intersected(area)
            if band.width() > 0 and band.height() > 0:
                painter.drawRect(band)
        painter.setBrush(Qt.NoBrush)

    def _draw_text(self, painter: QPainter, shape: Shape) -> None:
        local = text_local_rect(shape)
        font = make_font(shape.font_size)
        metrics = QFontMetricsF(font)
        painter.save()
        painter.translate(shape.pos)
        painter.translate(local.center())
        painter.rotate(shape.angle)
        painter.translate(-local.center())
        painter.setFont(font)
        painter.setPen(shape.color)
        for index, line in enumerate(shape.text.split("\n")):
            painter.drawText(
                QPointF(0, metrics.ascent() + index * metrics.lineSpacing()), line
            )
        painter.restore()

    def _draw_image(self, painter: QPainter, shape: Shape) -> None:
        if shape.pixmap is None or shape.pixmap.isNull():
            return
        width = shape.image_size.width()
        height = shape.image_size.height()
        painter.save()
        painter.translate(shape.pos)
        painter.translate(width / 2, height / 2)
        painter.rotate(shape.angle)
        painter.translate(-width / 2, -height / 2)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(
            QRectF(0, 0, width, height),
            shape.pixmap,
            QRectF(shape.pixmap.rect()),
        )
        painter.restore()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if not self.bg.isNull():
            painter.drawPixmap(0, 0, self.bg)
        if not self.ink.isNull():
            painter.drawPixmap(0, 0, self.ink)

        if self.live is not None:
            self.draw_shape(painter, self.live)
            # 橡皮预览用的是 Clear，画完必须复位，否则下面的框选/手柄/HUD 会被擦掉
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        if self.marquee is not None:
            rect = QRectF(self.marquee).normalized()
            highlight_mode = self.marquee_tool == "highlight"
            pen = QPen(
                QColor(255, 214, 10) if highlight_mode else SELECT_COLOR,
                1.5, Qt.DashLine,
            )
            painter.setPen(pen)
            painter.setBrush(
                QColor(255, 214, 10, 30) if highlight_mode else QColor(10, 132, 255, 30)
            )
            painter.drawRect(rect)

        if self.selection:
            bbox = self.selection_bbox()
            painter.setPen(QPen(SELECT_COLOR, 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(bbox.adjusted(-2, -2, 2, 2))
            handle = self.rotate_handle_pos()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 235))
            painter.drawEllipse(handle, 8, 8)
            painter.setBrush(SELECT_COLOR)
            painter.drawEllipse(handle, 5, 5)
            painter.setBrush(Qt.NoBrush)

        self._draw_hud(painter)

    # ------------------------------------------------------------------ HUD

    def _toolbar_geometry(self):
        panel_width = 24 + len(PALETTE) * 30 + 12 + 3 * 30 + 12
        panel = QRectF(
            (self.width() - panel_width) / 2, self.height() - 62, panel_width, 46
        )
        swatches = []
        x = panel.left() + 24
        for color in PALETTE:
            swatches.append((QRectF(x, panel.center().y() - 9, 18, 18), QColor(color)))
            x += 30
        widths = []
        x = panel.right() - 24 - 3 * 30
        for width in (2.0, 4.0, 8.0):
            widths.append((QRectF(x, panel.center().y() - 12, 24, 24), width))
            x += 30
        return panel, swatches, widths

    def _draw_hud(self, painter: QPainter) -> None:
        hint = (
            "1 画笔　2 橡皮　3 高亮　|　A 全选　X 删除　Z 撤销　V 粘贴　"
            "C 保存缓存并退出　|　RCtrl / ESC 退出　|　滚轮调粗细"
        )
        font = make_font(14)
        metrics = QFontMetricsF(font)
        text_width = metrics.horizontalAdvance(hint)
        box = QRectF(16, 14, text_width + 26, 34)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 155))
        painter.drawRoundedRect(box, 8, 8)
        painter.setFont(font)
        painter.setPen(HINT_COLOR)
        painter.drawText(box, Qt.AlignCenter, hint)

        panel, swatches, widths = self._toolbar_geometry()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(24, 24, 26, 200))
        painter.drawRoundedRect(panel, 14, 14)

        for rect, color in swatches:
            painter.setBrush(color)
            painter.drawEllipse(rect)
            if color == self.pen_color:
                painter.setPen(QPen(QColor(255, 255, 255, 240), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(rect.adjusted(-3, -3, 3, 3))
                painter.setPen(Qt.NoPen)

        for rect, width in widths:
            diameter = {2.0: 8, 4.0: 12, 8.0: 16}[width]
            active = abs(self.pen_width - width) < 0.01
            painter.setBrush(QColor(255, 255, 255, 235) if active else QColor(255, 255, 255, 110))
            painter.drawEllipse(rect.center(), diameter / 2, diameter / 2)
        painter.setBrush(Qt.NoBrush)

    def _handle_toolbar_click(self, pos: QPointF) -> bool:
        panel, swatches, widths = self._toolbar_geometry()
        if not panel.contains(pos):
            return False
        for rect, color in swatches:
            if rect.contains(pos):
                self.pen_color = QColor(color)
                self.update()
                return True
        for rect, width in widths:
            if rect.contains(pos):
                self.pen_width = width
                self.update()
                return True
        return True  # 点在面板空白处，也吞掉，避免误画

    # ------------------------------------------------------------------ 工具

    def set_tool(self, tool: str) -> None:
        self.tool = "select" if self.tool == tool else tool
        if self.tool != "select":
            self.selection.clear()
        self.live = None
        self.marquee = None
        self.setCursor(Qt.ArrowCursor if self.tool == "select" else Qt.CrossCursor)
        self.update()

    def eraser_width(self) -> float:
        return max(ERASER_MIN_WIDTH, self.pen_width * 3)

    # ------------------------------------------------------------------ 选区

    def selection_bbox(self) -> QRectF:
        rect = QRectF()
        for shape in self.selection:
            rect = rect.united(shape.bbox())
        return rect

    def rotate_handle_pos(self) -> QPointF:
        bbox = self.selection_bbox()
        return QPointF(bbox.center().x(), bbox.top() - 26)

    def select_all(self) -> None:
        self.tool = "select"
        self.setCursor(Qt.ArrowCursor)
        self.selection = [s for s in self.shapes if s.kind != "eraser"]
        self.update()

    def delete_selection(self) -> None:
        if not self.selection:
            return
        self.push_undo()
        selected = set(id(s) for s in self.selection)
        self.shapes = [s for s in self.shapes if id(s) not in selected]
        self.selection.clear()
        self.render_ink()

    # ------------------------------------------------------------------ 撤销

    def push_undo(self) -> None:
        self.undo_stack.append([s.copy() for s in self.shapes])
        if len(self.undo_stack) > UNDO_LIMIT:
            self.undo_stack.pop(0)

    def undo(self) -> None:
        if not self.undo_stack:
            return
        self.shapes = self.undo_stack.pop()
        self.selection.clear()
        self.live = None
        self.render_ink()

    # ------------------------------------------------------------------ 粘贴

    def paste_clipboard(self) -> None:
        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None:
            return
        cursor = self.mapFromGlobal(QCursor.pos())
        origin = QPointF(cursor)

        if mime.hasUrls():
            return                      # 文件不粘贴
        if mime.hasImage():
            image = clipboard.image()
            if image.isNull():
                return
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(1.0)
            dpr = self.ink.devicePixelRatio() or 1.0
            # 以物理像素 1:1 显示，避免高分屏下被放大
            size = QSizeF(pixmap.width() / dpr, pixmap.height() / dpr)
            max_w, max_h = self.width() * 0.9, self.height() * 0.9
            scale = 1.0
            if size.width() > max_w:
                scale = min(scale, max_w / size.width())
            if size.height() > max_h:
                scale = min(scale, max_h / size.height())
            size = QSizeF(size.width() * scale, size.height() * scale)
            shape = Shape("image")
            shape.pixmap = pixmap
            shape.image_size = size
            shape.pos = self._clamp_pos(origin, size)
        elif mime.hasText():
            text = clipboard.text()
            if not text.strip():
                return
            shape = Shape("text")
            shape.text = text
            shape.font_size = DEFAULT_FONT_SIZE
            shape.color = QColor(self.pen_color)
            shape.pos = self._clamp_pos(origin, text_local_rect(shape).size())
        else:
            return

        self.push_undo()
        self.shapes.append(shape)
        self.tool = "select"
        self.setCursor(Qt.ArrowCursor)
        self.selection = [shape]
        self.render_ink()

    def _clamp_pos(self, pos: QPointF, size) -> QPointF:
        max_x = max(0.0, self.width() - size.width())
        max_y = max(0.0, self.height() - size.height())
        return QPointF(min(max(0.0, pos.x()), max_x), min(max(0.0, pos.y()), max_y))

    # ------------------------------------------------------------------ 按键

    def handle_key(self, key: str) -> None:
        if key in ("toggle", "esc"):
            self.close_overlay(save=False)
            return
        if not self.active:
            return
        if key == "1":
            self.set_tool("pen")
        elif key == "2":
            self.set_tool("eraser")
        elif key == "3":
            self.set_tool("highlight")
        elif key == "a":
            self.select_all()
        elif key == "x":
            self.delete_selection()
        elif key == "z":
            self.undo()
        elif key == "v":
            self.paste_clipboard()
        elif key == "c":
            self.close_overlay(save=True)

    # ------------------------------------------------------------------ 鼠标

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.active or event.button() != Qt.LeftButton:
            return
        pos = event.position()
        if self._handle_toolbar_click(pos):
            return

        self.press_pos = QPointF(pos)
        self.last_pos = QPointF(pos)
        self.drag_dirty = False

        if self.tool == "pen":
            self.live = Shape("stroke")
            self.live.color = QColor(self.pen_color)
            self.live.width = self.pen_width
            self.live.points = [QPointF(pos)]
        elif self.tool == "eraser":
            self.live = Shape("eraser")
            self.live.width = self.eraser_width()
            self.live.points = [QPointF(pos)]
            self._erase_segment(pos, pos)
        elif self.tool == "highlight":
            self.marquee = QRectF(pos, pos)
            self.marquee_tool = "highlight"
        else:
            if self.selection:
                bbox = self.selection_bbox()
                handle = self.rotate_handle_pos()
                if self._distance(pos, handle) <= 13:
                    self.drag_mode = "rotate"
                    self.rot_center = bbox.center()
                    self.rot_last = self._angle_to(pos, self.rot_center)
                    return
                if bbox.adjusted(-6, -6, 6, 6).contains(pos):
                    self.drag_mode = "move"
                    return
            self.selection.clear()
            self.drag_mode = "marquee"
            self.marquee = QRectF(pos, pos)
            self.marquee_tool = "select"
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self.active:
            return
        pos = QPointF(event.position())

        if self.tool == "pen" and self.live is not None:
            if self._distance(pos, self.live.points[-1]) >= 1.2:
                self.live.points.append(pos)
                self.update()
        elif self.tool == "eraser" and self.live is not None:
            if self._distance(pos, self.live.points[-1]) >= 1.2:
                self._erase_segment(self.live.points[-1], pos)
                self.live.points.append(pos)
        elif self.tool == "highlight" and self.marquee is not None:
            self.marquee = QRectF(self.press_pos, pos).normalized()
            self.update()
        elif self.drag_mode == "marquee" and self.marquee is not None:
            self.marquee = QRectF(self.press_pos, pos).normalized()
            self.update()
        elif self.drag_mode == "move":
            dx = pos.x() - self.last_pos.x()
            dy = pos.y() - self.last_pos.y()
            if dx or dy:
                if not self.drag_dirty:
                    self.push_undo()
                    self.drag_dirty = True
                for shape in self.selection:
                    shape.translate(dx, dy)
                self.last_pos = pos
                self.render_ink()
        elif self.drag_mode == "rotate":
            angle = self._angle_to(pos, self.rot_center)
            delta = angle - self.rot_last
            self.rot_last = angle
            if abs(delta) > 1e-4:
                if not self.drag_dirty:
                    self.push_undo()
                    self.drag_dirty = True
                for shape in self.selection:
                    shape.rotate_about(self.rot_center, delta)
                self.render_ink()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if not self.active or event.button() != Qt.LeftButton:
            return
        pos = QPointF(event.position())

        if self.tool == "pen" and self.live is not None:
            if len(self.live.points) == 1:
                self.live.points.append(QPointF(pos))
            if self.live.points:
                self.push_undo()
                self.shapes.append(self.live)
            self.live = None
            self.render_ink()
        elif self.tool == "eraser" and self.live is not None:
            self._erase_segment(self.live.points[-1], pos)
            self.live.points.append(pos)
            self.push_undo()
            self.shapes.append(self.live)
            self.live = None
            self.render_ink()
        elif self.tool == "highlight" and self.marquee is not None:
            rect = QRectF(self.marquee).normalized()
            if rect.width() >= 4 and rect.height() >= 4:
                shape = Shape("highlight")
                shape.rect = rect
                self.push_undo()
                # 遮罩全局只保留一个：重新框选即替换旧的，避免多层叠加变厚
                self.shapes = [s for s in self.shapes if s.kind != "highlight"]
                self.selection = [s for s in self.selection if s.kind != "highlight"]
                self.shapes.append(shape)
            self.marquee = None
            self.render_ink()
        elif self.drag_mode == "marquee" and self.marquee is not None:
            rect = QRectF(self.marquee).normalized()
            self.marquee = None
            if rect.width() >= 4 or rect.height() >= 4:
                self.selection = [
                    s for s in self.shapes
                    if s.kind != "eraser" and s.bbox().intersects(rect)
                ]
            else:
                # 轻点：选中该点下最上层的对象
                hit = None
                for shape in reversed(self.shapes):
                    if shape.kind != "eraser" and shape.bbox().contains(pos):
                        hit = shape
                        break
                self.selection = [hit] if hit else []
            self.drag_mode = None
            self.update()
        else:
            self.drag_mode = None
            self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.active:
            return
        step = 1 if event.angleDelta().y() > 0 else -1
        self.pen_width = max(1.0, min(30.0, self.pen_width + step))
        self.update()

    def _erase_segment(self, start: QPointF, end: QPointF) -> None:
        painter = QPainter(self.ink)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        pen = QPen(QColor(0, 0, 0), self.eraser_width())
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        if start == end:                 # 单击也能擦出一个圆点
            painter.setBrush(QColor(0, 0, 0))
            radius = self.eraser_width() / 2
            painter.drawEllipse(start, radius, radius)
        else:
            painter.drawLine(start, end)
        painter.end()
        self.update()

    @staticmethod
    def _distance(a: QPointF, b: QPointF) -> float:
        return math.hypot(a.x() - b.x(), a.y() - b.y())

    @staticmethod
    def _angle_to(point: QPointF, center: QPointF) -> float:
        return math.degrees(math.atan2(point.y() - center.y(), point.x() - center.x()))


# ---------------------------------------------------------------------------
# 全局热键：回调只入队，主线程定时消费（避免跨线程操作 Qt）
# ---------------------------------------------------------------------------


class HotkeyManager:
    """全局热键：右 Ctrl 开关面板；面板内 1/2/3/A/X/Z/C/V/ESC 直接生效。

    这里用 pynput 的底层钩子，而不是 keyboard 库，原因有两个：
    1. keyboard 库按扫描码匹配按键，而左 Ctrl 和右 Ctrl 的扫描码都是 29，
       所以注册“右 Ctrl”后按左 Ctrl 也会触发（实测两个回调都会收到）。
    2. keyboard 库的 suppress=True 会把修饰键的按下/抬起一起吞掉，
       系统里 Ctrl 的按键状态会卡在“按下”，别的程序就以为你一直按着 Ctrl
      （编辑器里滚轮会一直缩放）。
    pynput 按虚拟键码区分（左 162 / 右 163），只认右 Ctrl，而且只吞面板内
    的会话按键，从不吞修饰键。
    """

    # 会话按键的虚拟键码 -> 队列里的名字
    _VK_TO_KEY = {
        0x31: "1", 0x32: "2", 0x33: "3",
        0x41: "a", 0x58: "x", 0x5A: "z", 0x43: "c", 0x56: "v",
        0x1B: "esc",
    }
    _WM_KEYDOWN = 0x0100
    _WM_KEYUP = 0x0101

    def __init__(self) -> None:
        self.queue: queue.Queue[str] = queue.Queue()
        self.active = False
        self._ctrl_down = False          # 右 Ctrl 是否已按下（过滤长按重复）
        self._down: set[str] = set()     # 当前按住的会话键（过滤长按重复）
        self._listener = pynput_keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            win32_event_filter=self._win32_filter,
        )
        self._listener.start()

    def _on_press(self, key) -> None:
        if key == pynput_keyboard.Key.ctrl_r:
            if not self._ctrl_down:
                self._ctrl_down = True
                self.queue.put("toggle")

    def _on_release(self, key) -> None:
        if key == pynput_keyboard.Key.ctrl_r:
            self._ctrl_down = False

    def _win32_filter(self, msg, data) -> bool:
        """面板打开时吞掉会话按键，避免输入落到下面的程序。

        在钩子里返回 False 吞掉事件时 pynput 不会再派发给 on_press，
        所以这里顺手把按键放进队列。
        """
        if not self.active:
            return True
        name = self._VK_TO_KEY.get(int(data.vkCode))
        if name is None:
            return True
        if msg == self._WM_KEYDOWN:
            if name not in self._down:
                self._down.add(name)
                self.queue.put(name)
            return False
        if msg == self._WM_KEYUP:
            self._down.discard(name)
            return False
        return True

    def set_session(self, active: bool) -> None:
        self.active = active
        if not active:
            self._down.clear()

    def shutdown(self) -> None:
        try:
            self._listener.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 托盘图标
# ---------------------------------------------------------------------------


def make_tray_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(28, 28, 30))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 16, 16)

    pen = QPen(QColor(255, 69, 58), 9)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(22, 42), QPointF(43, 21))

    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(255, 214, 10))
    painter.drawEllipse(QPointF(20, 44), 7, 7)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------


class App(QObject):
    def __init__(self, qt_app: QApplication) -> None:
        super().__init__()
        self.qt_app = qt_app
        self.overlay = Overlay()
        self.hotkeys = HotkeyManager()
        self._last_toggle = 0.0

        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("屏幕画图 · 右 Ctrl 打开")
        menu = QMenu()
        action_toggle = menu.addAction("打开标注（右 Ctrl）")
        action_toggle.triggered.connect(self.toggle_overlay)
        menu.addSeparator()
        action_quit = menu.addAction("退出")
        action_quit.triggered.connect(self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.timer = QTimer(self)
        self.timer.setInterval(25)
        self.timer.timeout.connect(self._drain_hotkeys)
        self.timer.start()

        self.tray.showMessage(
            "屏幕画图已就绪", "按右 Ctrl 开始标注；C 保存缓存并退出", QSystemTrayIcon.Information, 4000
        )

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.toggle_overlay()

    def toggle_overlay(self) -> None:
        now = time.time()
        if now - self._last_toggle < 0.35:
            return
        self._last_toggle = now
        if self.overlay.active:
            self.overlay.close_overlay(save=False)
            self.hotkeys.set_session(False)
        else:
            self.hotkeys.set_session(True)
            self.overlay.open_overlay()

    def _drain_hotkeys(self) -> None:
        while True:
            try:
                key = self.hotkeys.queue.get_nowait()
            except queue.Empty:
                return
            if key == "toggle":
                self.toggle_overlay()
                continue
            if self.overlay.active:
                self.overlay.handle_key(key)
                if not self.overlay.active:
                    self.hotkeys.set_session(False)

    def quit(self) -> None:
        self.hotkeys.shutdown()
        self.tray.hide()
        self.qt_app.quit()


def main() -> int:
    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)
    qt_app.setWindowIcon(make_tray_icon())
    app = App(qt_app)
    # 防止对象被回收
    qt_app._screen_draw_app = app  # type: ignore[attr-defined]
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())

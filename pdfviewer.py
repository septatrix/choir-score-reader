import sys
import json
import os
import fitz  # PyMuPDF
from PySide6.QtWidgets import (
    QApplication, QLabel, QScrollArea, QMainWindow, QFileDialog, QToolBar
)
from PySide6.QtGui import QPixmap, QImage, QPainter, QPen, QIcon, QAction
from PySide6.QtCore import Qt, QTimer


class PDFViewer(QMainWindow):
    def __init__(self, pdf_path):
        super().__init__()
        self.setWindowTitle(f"Smooth PDF Viewer – {os.path.basename(pdf_path)}")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFocus()

        # store PDF info
        self.pdf_path = pdf_path
        self.anchor_path = self._make_anchor_filename(pdf_path)
        self.doc = fitz.open(pdf_path)

        self.base_pdf_scale = 2.0     # the scale used to generate base_image
        self.user_scale = 1.0         # the zoom factor the UI controls
        self.current_render_scale = self.base_pdf_scale * self.user_scale
        self.fit_to_width = True
        self.performance_mode = False
        self.anchors = []
        self.current_anchor_index = -1


        # Render base image (full resolution)
        self.base_image = self.render_pdf_to_long_image(self.doc)
        self.image = self.base_image

        # UI elements
        self.label = ClickableLabel(self)
        self.label.setPixmap(QPixmap.fromImage(self.image))
        self.label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.label)
        self.scroll_area.setWidgetResizable(True)
        self.setCentralWidget(self.scroll_area)

        # Toolbar for zoom/reset
        self.toolbar = QToolBar("Tools")
        self.addToolBar(self.toolbar)

        act_fit = QAction(QIcon(), "Fit Width", self)
        act_fit.triggered.connect(self.reset_zoom)
        self.toolbar.addAction(act_fit)

        # Render zoom cache
        self.render_cache = {}


        # Timer for smooth scroll
        self.timer = QTimer()
        self.timer.timeout.connect(self.smooth_scroll)
        self.target_y = 0

        # Load anchors automatically if available
        self.load_anchors()

        # Draw anchors
        self.update_pixmap()


    def _make_anchor_filename(self, pdf_path):
        base = os.path.splitext(os.path.basename(pdf_path))[0]
        return f"anchors_{base}.json"

    # ---------- PDF Rendering ----------
    def render_pdf_to_long_image(self, doc):
        """Render all pages into one tall QImage at base zoom (2.0)."""
        zoom = 2.0
        mat = fitz.Matrix(zoom, zoom)
        images = []
        widths, heights = [], []

        for page in doc:
            pix = page.get_pixmap(matrix=mat)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
            images.append(img.copy())
            widths.append(pix.width)
            heights.append(pix.height)

        total_height = sum(heights)
        total_width = max(widths)
        long_image = QImage(total_width, total_height, QImage.Format.Format_RGB888)
        long_image.fill(Qt.GlobalColor.white)

        painter = QPainter(long_image)
        y_offset = 0
        for img in images:
            painter.drawImage(0, y_offset, img)
            y_offset += img.height()
        painter.end()

        return long_image

    def render_pdf_scaled(self, doc, render_scale):
        """Render all pages into one tall QImage at the given MuPDF render_scale."""
        mat = fitz.Matrix(render_scale, render_scale)
        images = []
        widths, heights = [], []

        for page in doc:
            pix = page.get_pixmap(matrix=mat)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
            images.append(img.copy())
            widths.append(pix.width)
            heights.append(pix.height)

        total_height = sum(heights)
        total_width = max(widths)
        long_image = QImage(total_width, total_height, QImage.Format.Format_RGB888)
        long_image.fill(Qt.GlobalColor.white)

        painter = QPainter(long_image)
        y_offset = 0
        for img in images:
            painter.drawImage(0, y_offset, img)
            y_offset += img.height()
        painter.end()

        return long_image


    # ---------- Scaling and Updating ----------
    def update_scaled_image(self):
        """
        Re-render PDF at current zoom or fit-to-width, keeping the *top line* fixed.
        Uses explicit render scales to avoid confusion between 'zoom' semantics.
        """
        sb = self.scroll_area.verticalScrollBar()

        # current render scale (pixels-per-PDF-unit) used to display the current image
        old_render_scale = getattr(self, "current_render_scale", self.base_pdf_scale * self.user_scale)

        # 1) Compute absolute PDF Y coordinate currently at the top of the viewport
        y_top_scaled = sb.value()               # top of viewport, in *pixels of current image*
        y_top_pdf = y_top_scaled / old_render_scale if old_render_scale != 0 else 0.0

        # 2) Update user_scale if fit-to-width requested
        if self.fit_to_width:
            target_width = self.scroll_area.viewport().width()
            # base_image.width() equals (pdf_width * base_pdf_scale)
            # user_scale should be ratio of target_width to base_image width
            self.user_scale = target_width / max(1, self.base_image.width())

        # 3) Compute new render scale and re-render
        new_render_scale = self.base_pdf_scale * self.user_scale
        self.current_render_scale = new_render_scale
        print(f"Re-rendering PDF at render_scale={new_render_scale:.3f} (user_scale={self.user_scale:.3f})")
        self.image = self.render_pdf_scaled(self.doc, new_render_scale)
        self.update_pixmap()

        # 4) After layout settles, restore the vertical scroll so the same PDF Y is at the top.
        # Use singleShot to ensure scroll ranges updated. Clamp to scrollbar range.
        def _restore():
            QApplication.processEvents()   # give Qt a chance to update layout and scrollbar ranges
            sb2 = self.scroll_area.verticalScrollBar()
            target_scaled = int(round(y_top_pdf * self.current_render_scale))
            # clamp to valid range
            target_scaled = max(0, min(target_scaled, sb2.maximum()))
            sb2.setValue(target_scaled)
        QTimer.singleShot(0, _restore)

    def _restore_top_position(self, y_top_base):
        """Scroll so that the same document Y position stays at top."""
        sb = self.scroll_area.verticalScrollBar()
        target_y_scaled = int(y_top_base * self.zoom)
        sb.setValue(target_y_scaled)

    def update_pixmap(self):
        """Redraw anchors on top of scaled image and refresh immediately."""
        pixmap = QPixmap.fromImage(self.image.copy())
        painter = QPainter(pixmap)

        # only draw anchors if not in performance mode
        if not getattr(self, "performance_mode", False):
            pen = QPen(Qt.GlobalColor.red, 3)
            painter.setPen(pen)
            for y in self.anchors:
                painter.drawLine(0, int(y * self.user_scale), pixmap.width(), int(y * self.user_scale))

        painter.end()
        self.label.setPixmap(pixmap)
        self.label.adjustSize()
        self.label.update()
        self.scroll_area.viewport().update()
        QApplication.processEvents()



    def resizeEvent(self, event):
        """Auto-rescale when in fit-to-width mode."""
        super().resizeEvent(event)
        if self.fit_to_width:
            self.update_scaled_image()

    # ---------- Anchor Management ----------
    def add_anchor(self, y_click):
        """Add anchor at correct Y coordinate (accounting for zoom).
        IMPORTANT: event.pos().y() is already in the label's content coords,
        so don't add the scrollbar value again.
        """
        # y_click is already relative to the full label (scaled) coordinates
        y_abs = y_click / self.user_scale  # convert back to base-image coords
        # clamp just in case
        y_abs = max(0, min(self.base_image.height() - 1, y_abs))
        self.anchors.append(y_abs)
        self.anchors = sorted(list(set(self.anchors)))
        print(f"Added anchor at base-y={y_abs:.1f} (click scaled-y={y_click})")
        self.update_pixmap()

    def remove_nearest_anchor(self, y_click):
        """Remove the anchor nearest to the clicked Y position (converted from scaled coords)."""
        if not self.anchors:
            return
        y_abs = y_click / self.user_scale
        nearest = min(self.anchors, key=lambda a: abs(a - y_abs))
        self.anchors.remove(nearest)
        print(f"Removed anchor near base-y={y_abs:.1f} (actual {nearest:.1f})")
        self.update_pixmap()


    def keyPressEvent(self, event):
        key = event.key()

        # Performance mode: only ESC works
        if getattr(self, "performance_mode", False):
            if key == Qt.Key.Key_Escape:
                self.exit_performance_mode()
            elif key == Qt.Key.Key_Right or key == Qt.Key.Key_PageDown:
                self.next_anchor()
            elif key == Qt.Key.Key_Left or key == Qt.Key.Key_PageUp:
                self.prev_anchor()
            elif key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal or key == Qt.Key.Key_P:
                self.zoom_in()
            elif key == Qt.Key.Key_Minus or key == Qt.Key.Key_M:
                self.zoom_out()
            return

        # --- Normal mode controls ---
        elif key == Qt.Key.Key_S:
            self.save_anchors()
        elif key == Qt.Key.Key_Right or key == Qt.Key.Key_PageDown:
            self.next_anchor()
        elif key == Qt.Key.Key_Left or key == Qt.Key.Key_PageUp:
            self.prev_anchor()
        elif key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal or key == Qt.Key.Key_P:
            self.zoom_in()
        elif key == Qt.Key.Key_Minus or key == Qt.Key.Key_M:
            self.zoom_out()
        elif key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            self.add_anchor_at_view_top()
        elif key == Qt.Key.Key_F:
            self.enter_performance_mode()
        elif key == Qt.Key.Key_Escape:
            sys.exit()


    def add_anchor_at_view_top(self):
        """Add an anchor corresponding to the current top of the visible area."""
        if getattr(self, "performance_mode", False):
            return  # disabled in performance mode

        sb = self.scroll_area.verticalScrollBar()
        y_top_scaled = sb.value()  # top of the viewport in scaled coordinates
        y_abs = y_top_scaled / self.user_scale  # convert to base-image coordinate
        self.anchors.append(y_abs)
        self.anchors = sorted(list(set(self.anchors)))
        print(f"Added anchor at top of view (base-y={y_abs:.1f})")
        self.update_pixmap()

    def next_anchor(self):
        if not self.anchors:
            print("No anchors set!")
            return
        if self.current_anchor_index < len(self.anchors) - 1:
            self.current_anchor_index += 1
        else:
            self.current_anchor_index = 0
        self.scroll_to_anchor(self.current_anchor_index)

    def prev_anchor(self):
        if not self.anchors:
            print("No anchors set!")
            return
        if self.current_anchor_index > 0:
            self.current_anchor_index -= 1
        else:
            self.current_anchor_index = len(self.anchors) - 1
        self.scroll_to_anchor(self.current_anchor_index)

    def scroll_to_anchor(self, index):
        target_y = int(self.anchors[index] * self.user_scale)
        sb = self.scroll_area.verticalScrollBar()
        self.start_y = sb.value()
        self.target_y = min(max(0, target_y), sb.maximum())

        # Fewer frames = faster travel; higher speed = snappier feel
        frames = 30            # ↓ fewer frames (was 20)
        duration_ms = 200      # total animation time
        interval = duration_ms / frames
        self.speed = (self.target_y - self.start_y) / frames

        self.timer.start(int(interval))
        print(f"Scrolling quickly to anchor {index}: {target_y/self.user_scale:.1f} (zoom={self.user_scale:.2f})")

    def smooth_scroll(self):
        sb = self.scroll_area.verticalScrollBar()
        new_val = sb.value() + self.speed
        if (self.speed > 0 and new_val >= self.target_y) or (self.speed < 0 and new_val <= self.target_y):
            sb.setValue(int(self.target_y))
            self.timer.stop()
        else:
            sb.setValue(int(new_val))

    # ---------- Zoom ----------
    def zoom_in(self):
        self.fit_to_width = False
        self.user_scale *= 1.85
        print(f"Zoom In → user_scale={self.user_scale:.3f}")
        self.update_scaled_image()

    def zoom_out(self):
        self.fit_to_width = False
        self.user_scale /= 1.85
        print(f"Zoom Out → user_scale={self.user_scale:.3f}")
        self.update_scaled_image()

    def reset_zoom(self):
        """Fit to window width."""
        self.fit_to_width = True
        # update_scaled_image will compute user_scale based on viewport width
        self.update_scaled_image()
        print("Fit-to-width mode enabled")

    # ---------- Save/Load ----------
    def save_anchors(self):
        """Save anchors automatically to anchors_<pdfname>.json."""
        with open(self.anchor_path, "w") as f:
            json.dump(self.anchors, f, indent=2)
        print(f"💾 Anchors saved to {self.anchor_path}")

    def load_anchors(self):
        """Load anchors automatically from anchors_<pdfname>.json, if exists."""
        if not os.path.exists(self.anchor_path):
            print(f"(no anchors file found for {self.pdf_path})")
            return
        with open(self.anchor_path, "r") as f:
            self.anchors = json.load(f)
        self.current_anchor_index = -1
        print(f"📂 Loaded {len(self.anchors)} anchors from {self.anchor_path}")
        self.update_pixmap()


    # ---------- Performance Mode ----------
    def enter_performance_mode(self):
        """Hide anchors and toolbar, disable interactions, go fullscreen."""
        self.performance_mode = True
        self.showFullScreen()
        if hasattr(self, "toolbar"):
            self.toolbar.setVisible(False)
        self.update_pixmap()
        print("🎬 Entered Performance Mode (press ESC to exit)")

    def exit_performance_mode(self):
        """Restore normal interactive mode."""
        self.performance_mode = False
        self.showMaximized()
        if hasattr(self, "toolbar"):
            self.toolbar.setVisible(True)
        self.update_pixmap()
        print("⬅️ Exited Performance Mode")





class ClickableLabel(QLabel):
    def __init__(self, parent_viewer):
        super().__init__()
        self.parent_viewer = parent_viewer
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event):
        if getattr(self.parent_viewer, "performance_mode", False):
            # Ignore all clicks in performance mode
            return

        if event.button() == Qt.MouseButton.LeftButton:
            self.parent_viewer.add_anchor(event.pos().y())
        elif event.button() == Qt.MouseButton.RightButton:
            self.parent_viewer.remove_nearest_anchor(event.pos().y())
        self.setFocus()

    def keyPressEvent(self, event):
        self.parent_viewer.keyPressEvent(event)

if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)

    if len(sys.argv) < 2:
        print("Usage: python pdf_viewer.py <file.pdf>")
        sys.exit(1)

    pdf_file = sys.argv[1]
    if not os.path.exists(pdf_file):
        print(f"File not found: {pdf_file}")
        sys.exit(1)

    viewer = PDFViewer(pdf_file)
    viewer.showMaximized()
    sys.exit(app.exec())

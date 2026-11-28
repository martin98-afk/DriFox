"""后台读取聊天附件，向主线程逐张交付缩略图。"""

from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, Qt, QThreadPool, pyqtSignal
from PyQt5.QtGui import QImage, QImageReader
from loguru import logger

from app.widgets.card_viewers import iter_image_attachment_sources

_pool: QThreadPool | None = None


def _read_thumbnail(source, data_uri, dpr: float) -> QImage:
    if source:
        reader = QImageReader(source)
    else:
        encoded = data_uri.split("base64,", 1)[-1]
        buffer = QBuffer()
        buffer.setData(QByteArray.fromBase64(encoded.encode("ascii")))
        buffer.open(QIODevice.ReadOnly)
        reader = QImageReader(buffer)
    height = max(1, int(80 * dpr))
    size = reader.size()
    if size.isValid() and size.height() > height:
        reader.setScaledSize(QSize(max(1, round(size.width() * height / size.height())), height))
    image = reader.read()
    if not image.isNull() and image.height() != height:
        image = image.scaledToHeight(height, Qt.SmoothTransformation)
    image.setDevicePixelRatio(dpr)
    return image


class AttachmentThumbnails(QObject):
    loaded = pyqtSignal(QImage, object, object, str)
    finished = pyqtSignal(object)

    def __init__(self, paths, content, dpr):
        super().__init__()
        self._request = (list(paths), content)
        self._dpr = dpr
        self.cancelled = False

    def start(self):
        global _pool
        if _pool is None:
            # 部分格式即使请求小图仍需完整解码。所有窗口共享一个解码线程以限制峰值。
            _pool = QThreadPool()
            _pool.setMaxThreadCount(1)
        _pool.start(self._run)

    def cancel(self):
        self.cancelled = True
        self._request = None  # 排队期间即可释放会话内嵌的图片

    def _run(self):
        request = self._request
        self._request = None
        try:
            if request is None or self.cancelled:
                return
            for source, uri, path in iter_image_attachment_sources(*request):
                if self.cancelled:
                    break
                try:
                    image = _read_thumbnail(source, uri, self._dpr)
                except (ValueError, OSError):
                    continue
                if not self.cancelled and not image.isNull():
                    self.loaded.emit(image, source, uri, path)
        except Exception:
            logger.exception("读取附件缩略图失败")
        finally:
            self.finished.emit(self)

"""
Gửi cảnh báo qua Telegram Bot API. Hiện dùng cho ticket sắp vỡ SLA 48h
(xem sla_alert.py) - viết tách riêng để sau này muốn báo thêm việc khác
(vd. tất cả tài khoản CCTS đăng nhập thất bại, cào dữ liệu lỗi liên tục...)
chỉ cần gọi lại send_telegram_message(), không phải viết lại phần gọi API.

CÁCH LẤY TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (làm 1 lần):
  1. Mở Telegram, chat với @BotFather -> gõ /newbot -> đặt tên bot -> nhận
     về TOKEN dạng "123456789:ABC-xyz...". Đây là TELEGRAM_BOT_TOKEN.
  2. Nhắn bất kỳ tin nào cho bot vừa tạo (bấm Start).
  3. Mở trình duyệt: https://api.telegram.org/bot<TOKEN>/getUpdates
     (thay <TOKEN> bằng token ở bước 1) -> tìm số trong "chat":{"id": ...}
     -> đó là TELEGRAM_CHAT_ID.
  4. Vào Render > service này > Environment -> thêm 2 biến trên -> Deploy
     lại (Render tự deploy khi đổi Environment).

Muốn báo vào 1 GROUP thay vì chat cá nhân: thêm bot vào group, chat_id của
group thường là số ÂM (vd -1001234567890) - lấy tương tự bằng getUpdates
sau khi bot đã ở trong group và có người nhắn gì đó trong group.
"""
import os

import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

_API_BASE = "https://api.telegram.org"


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_telegram_message(text: str) -> bool:
    """Gửi 1 tin nhắn Telegram (HTML). Trả về True nếu gửi thành công.

    KHÔNG raise exception ra ngoài - lỗi mạng/Telegram/token sai chỉ log ra
    console, tuyệt đối không được làm gãy vòng lặp cào dữ liệu chính (đây
    chỉ là tính năng phụ trợ, app phải chạy tiếp bình thường dù Telegram
    có trục trặc)."""
    if not is_configured():
        print("[telegram] Chưa cấu hình TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID - bỏ qua gửi cảnh báo.")
        return False

    url = f"{_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[telegram] Gửi thất bại (HTTP {resp.status_code}): {resp.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[telegram] Lỗi khi gửi tin nhắn: {e!r}")
        return False

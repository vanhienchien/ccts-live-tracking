"""
Tự động phát hiện ticket SẮP VỠ SLA 48h và báo qua Telegram.

Dữ liệu is_near_overdue (45 <= hours < 48, tức còn ~0-3h trước khi chạm mốc
SLA 48h) đã được ccts_data._build_ticket_rows() tính sẵn cho mỗi ticket -
module này chỉ lo 2 việc: (1) phát hiện ticket MỚI rơi vào diện đó so với
lần kiểm tra trước, (2) gửi Telegram, tránh báo trùng lặp mỗi chu kỳ cào.

Gọi check_and_alert(ticket_rows) SAU MỖI lần refresh_stations_once() cào
thành công (main.py) - KHÔNG gọi lúc app khởi động nạp cache cũ, vì đó là
dữ liệu cũ chứ không phải phát hiện mới.
"""
from telegram_notify import send_telegram_message

APP_URL = "https://ccts-live-tracking.onrender.com"

# Ticket đã báo rồi thì không báo lại (chỉ lưu trong RAM - mất khi Render
# restart, chấp nhận được: thà thỉnh thoảng báo lại 1 ticket còn hơn bỏ sót).
_alerted_ids: set[str] = set()

# Giới hạn số ticket liệt kê chi tiết trong 1 tin nhắn (đề phòng trường hợp
# hiếm: rất nhiều ticket cùng rơi vào diện sắp vỡ hạn 1 lượt, vd sau khi
# app tạm ngưng cào 1 thời gian dài) - tránh vượt giới hạn 4096 ký tự của
# Telegram và tin nhắn quá dài khó đọc.
_MAX_LISTED = 15


def check_and_alert(ticket_rows: list[dict]) -> None:
    global _alerted_ids

    near_overdue_now = {
        str(r["ticket_id"]): r
        for r in ticket_rows
        if r.get("is_near_overdue") and r.get("ticket_id")
    }

    new_ids = set(near_overdue_now) - _alerted_ids
    # Ticket không còn "sắp vỡ hạn" nữa (đã xử lý xong / trôi qua luôn mốc
    # 48h / ticket đóng) -> quên đi, để nếu sau này rơi lại vào diện này
    # (vd ticket mở lại) vẫn được báo tiếp thay vì bị nhớ "đã báo" mãi mãi.
    gone_ids = _alerted_ids - set(near_overdue_now)
    if gone_ids:
        _alerted_ids -= gone_ids

    if not new_ids:
        return

    sorted_ids = sorted(new_ids)
    lines = [f"⏰ <b>{len(new_ids)} ticket SẮP VỠ SLA 48h</b> (còn dưới 3h):", ""]
    for tid in sorted_ids[:_MAX_LISTED]:
        r = near_overdue_now[tid]
        remaining = max(0.0, 48.0 - float(r.get("hours") or 0))
        station = r.get("station_code") or "?"
        cp = r.get("cp_id") or "?"
        tech = r.get("tech_name") or "Unassigned"
        address = (r.get("address") or "").strip()
        lines.append(
            f"🔴 <b>{tid}</b> · còn ~{remaining:.1f}h\n"
            f"   Trạm {station} · Trụ {cp}\n"
            f"   KT: {tech}"
            + (f"\n   {address}" if address else "")
        )
    if len(sorted_ids) > _MAX_LISTED:
        lines.append(f"\n… và {len(sorted_ids) - _MAX_LISTED} ticket khác, xem trên bản đồ.")

    lines.append("")
    lines.append(f"👉 {APP_URL}")

    if send_telegram_message("\n".join(lines)):
        _alerted_ids |= new_ids
    # Gửi thất bại (Telegram lỗi, chưa cấu hình...): KHÔNG thêm vào
    # _alerted_ids, để chu kỳ cào kế tiếp (~10 phút sau) tự thử báo lại.

CÀO THỐNG KÊ 0H QUA GITHUB ACTIONS — VIỆC CẦN LÀM
==================================================

1) Thêm Secrets vào repo chứa app/ (Settings > Secrets and variables >
   Actions > New repository secret) — lấy đúng giá trị từ file .env local
   của bạn:

   - CCTS_USERNAME_ES
   - CCTS_PASSWORD
   - GOOGLE_SERVICE_ACCOUNT_JSON
   - SPREADSHEET_URL
   - GITHUB_DATA_REPO
   - DATA_REPO_TOKEN            (chính là giá trị GITHUB_TOKEN trong .env —
                                  KHÔNG đặt tên secret là "GITHUB_TOKEN" vì
                                  GitHub reserve tên này, không cho tạo)
   - CACHE_S3_BUCKET
   - CACHE_S3_ACCESS_KEY
   - CACHE_S3_SECRET_KEY
   - CACHE_S3_ENDPOINT

2) Trên Render dashboard: đặt biến môi trường STATS_SCRAPE_ENABLED=0 (Render
   chỉ đọc cache, không tự cào lúc 0h/khởi động nữa).

3) Sau khi thêm xong Secrets: vào tab Actions > "Cào thống kê CCTS lúc 0h
   VN" > Run workflow để test tay 1 lần trước khi tin lịch tự động 00:05 VN
   mỗi ngày. Theo dõi log — nếu login CCTS/đẩy R2 đều OK là xong, không cần
   chạy tay ở local nữa.

4) File wake-before-midnight.yml đã được tắt lịch tự động (chỉ còn bấm tay)
   vì không còn cần thiết — có thể xoá hẳn nếu muốn, tôi không có quyền xoá
   file trên máy bạn qua kết nối hiện tại nên để bạn tự xoá nếu không cần.

GHI CHÚ RỦI RO
- Lịch GitHub Actions có thể trễ vài phút lúc GitHub tải cao (không đảm bảo
  chạy đúng giây 00:05:00).
- GitHub tự TẮT lịch cron nếu repo không có commit nào trong 60 ngày liên
  tục — nếu repo ít hoạt động, thỉnh thoảng kiểm tra tab Actions xem lịch
  còn chạy không.
- Chưa kiểm chứng CCTS có chặn theo IP/khu vực hay không — vì Render (cũng
  là IP nước ngoài) vẫn cào CCTS bình thường mỗi 15 phút nên khả năng cao
  GitHub Actions cũng không bị chặn, nhưng nên xác nhận qua bước test tay
  (mục 3) trước khi tin tưởng hoàn toàn.

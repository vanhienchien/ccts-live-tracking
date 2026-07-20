# CCTS Live Map (FastAPI + WebSocket)

Bản thay thế cho ứng dụng Streamlit trước đây — website độc lập, vị trí nhân sự
cập nhật mượt theo thời gian thực (không cần load lại trang), bản đồ trạm sạc
tự làm mới mỗi 10 phút.

## 1. Các file cần bạn tự chép vào (chưa có sẵn ở đây)

Copy nguyên các file sau từ project Streamlit cũ của bạn vào thư mục này:

- `api_client.py`
- `utils.py`
- `station_info.json`, `listLongLat.xlsx`, `list_Stations.json`, `ChargePoint_Model.xlsx`
  (các file dữ liệu tĩnh — file nào không có thì bỏ qua, code đã có try/except)

## 2. Cài đặt

```bash
pip install -r requirements.txt
```

## 3. Cấu hình (biến môi trường)

Tạo file `.env` ở thư mục gốc (không commit file này lên Git):

```env
SPREADSHEET_URL=https://docs.google.com/spreadsheets/d/xxxxxxxx/edit
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
CCTS_USERNAME=esmanager
CCTS_PASSWORD=Ccts123.
TICKET_REFRESH_SECONDS=600
```

Đặt file `service_account.json` (key Service Account đã Share Editor cho Google
Sheet) cùng thư mục. Khi deploy lên nền tảng không cho upload file (Render/
Railway...), dùng biến `GOOGLE_SERVICE_ACCOUNT_JSON` thay thế — dán **nguyên
văn nội dung file json thành 1 dòng** vào giá trị biến môi trường đó, không cần
`GOOGLE_SERVICE_ACCOUNT_FILE` nữa.

## 4. Cấu trúc Google Sheet "Users" (đơn giản hoá theo yêu cầu — không mã hoá mật khẩu)

Đúng 5 cột theo thứ tự:

| full_name | username | password | role | region |
|---|---|---|---|---|
| Nguyễn Văn A | vana | 123456 | Kỹ thuật | Miền Nam |
| Trần Thị B | tranb | abcdef | Điều phối khu vực | Miền Nam |
| Lê Văn C | levanc | xyz123 | Giám đốc | ALL |
| ... | ... | ... | ... | ... |

Cột `role` phải là 1 trong 5 giá trị (không phân biệt hoa/thường): **Kỹ thuật,
Điều phối khu vực, Điều hành, Giám đốc, Admin**. Bạn tự thêm/sửa/xoá dòng trực
tiếp trên Sheet — không còn giao diện "Quản lý tài khoản" trong web nữa.

Quy tắc xem vị trí: **Admin xem được toàn bộ** (kể cả Admin khác); các vai trò
khác chỉ xem được **cấp bậc thấp hơn mình**, không phân biệt khu vực.

> Sheet "Locations" của bản cũ **không còn cần thiết nữa** — vị trí giờ chỉ lưu
> tạm trong bộ nhớ server khi đang chạy (xem giải thích bên dưới).

## 5. Chạy thử local

**Trên Windows: KHÔNG dùng `--reload`** (xem giải thích bên dưới):

```powershell
uvicorn main:app --host 0.0.0.0 --port 8000
```

**Trên Linux/macOS**, có thể dùng `--reload` bình thường khi phát triển:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

> **Vì sao Windows không được dùng `--reload`?** Cờ `--reload` bật cơ chế theo
> dõi file thay đổi (WatchFiles), và trên Windows cơ chế này khiến asyncio
> chuyển sang dùng `SelectorEventLoop` thay vì `ProactorEventLoop` mặc định.
> `SelectorEventLoop` **không hỗ trợ tạo subprocess trên Windows**, trong khi
> Playwright Async API bắt buộc phải tạo subprocess để mở trình duyệt — dẫn
> đến lỗi `NotImplementedError` khi đăng nhập CCTS. `main.py` đã tự set
> `WindowsProactorEventLoopPolicy` ở đầu file để giảm thiểu vấn đề này, nhưng
> `--reload` vẫn có thể ép lại Selector sau đó, nên cách chắc chắn nhất là
> tắt hẳn `--reload` khi chạy trên Windows (mỗi lần sửa code thì tự restart
> thủ công). **Vấn đề này chỉ xảy ra trên Windows** — khi deploy lên Render
> (chạy Linux) sẽ không gặp lỗi này, có thể dùng `--reload` thoải mái nếu cần
> (dù trên production thường không cần reload).

Mở `http://localhost:8000` (lưu ý: trình duyệt chỉ cho phép lấy vị trí GPS qua
`https://` hoặc `localhost` — nên khi deploy thật cần có HTTPS, các nền tảng
miễn phí bên dưới đều tự cấp HTTPS sẵn).

## 6. Gợi ý tên miền/hosting miễn phí

Vì dữ liệu còn ít, có thể dùng ngay:

- **Render.com** (Free Web Service) — có subdomain miễn phí dạng
  `ten-app.onrender.com`, tự có HTTPS, hỗ trợ WebSocket. Nhược điểm: gói free sẽ
  "ngủ" sau ~15 phút không có ai truy cập, lần truy cập kế tiếp sẽ mất khoảng
  30–60s để "thức dậy".
- **Railway.app** — tương tự Render, subdomain `.up.railway.app`.
- **Fly.io** — subdomain `.fly.dev`, không ngủ nhưng cấu hình phức tạp hơn 1 chút.

Khi công ty duyệt chi phí, có thể mua domain riêng (vd `ccts.tencongty.com`) rồi
trỏ CNAME về 1 trong các nền tảng trên mà không cần sửa code.

## 7. Vì sao vị trí không lưu Google Sheets nữa?

Trước đây (bản Streamlit) mỗi lần vị trí đổi phải ghi lên Google Sheets — với
tần suất cập nhật liên tục (vài giây/lần) như bây giờ, việc này sẽ nhanh chóng
chạm giới hạn tốc độ (rate limit) của Google Sheets API, đặc biệt khi nhiều kỹ
thuật viên di chuyển cùng lúc. Vị trí giờ được giữ trong bộ nhớ server và phát
(broadcast) trực tiếp qua WebSocket tới những người có quyền xem — nhanh hơn
nhiều và không phụ thuộc Google Sheets. Cái giá phải trả: nếu server khởi động
lại, vị trí sẽ "trắng" cho tới khi mỗi người gửi ping tiếp theo (thường trong
vài giây, không đáng lo với ứng dụng theo dõi thời gian thực).
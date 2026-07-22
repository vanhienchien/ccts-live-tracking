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

## 8. Lấy vị trí liên tục kể cả khi tắt màn hình (Traccar Client)

**Vì sao trình duyệt không làm được:** iOS Safari và (ở mức độ ít hơn) Chrome
Android đều tạm dừng JavaScript của tab web khi tắt màn hình hoặc chuyển ứng
dụng khác, để tiết kiệm pin — đây là giới hạn của nền tảng, không phải lỗi
code. Không có cách nào để 1 trang web thuần lấy vị trí đáng tin cậy khi màn
hình tắt; việc này BẮT BUỘC cần 1 ứng dụng gốc (native) có quyền chạy nền.

**Giải pháp: dùng app có sẵn thay vì tự viết app mới** — [Traccar Client]
(https://www.traccar.org/client/) là ứng dụng mã nguồn mở, miễn phí, có sẵn
cho cả Android và iOS, được thiết kế đúng để giải quyết vấn đề này bằng các
API nền (background location) chính thức của từng hệ điều hành. App gửi vị trí
định kỳ tới 1 URL server bạn tự cấu hình — mình đã thêm sẵn 1 endpoint
(`/api/traccar`) tương thích với giao thức HTTP đơn giản của app này.

**Cách thiết lập cho từng kỹ thuật viên (chi tiết từng bước, đúng theo giao
diện thật của app):**

1. **Cài đặt biến môi trường trên server trước** (làm 1 lần, không phải trên
   điện thoại): thêm `TRACCAR_TOKEN` = 1 chuỗi bí mật tự chọn (vd chạy
   `openssl rand -hex 16` để tạo ngẫu nhiên) vào Environment Variables trên
   Render (hoặc `.env` khi chạy local). Dùng để chặn người lạ gửi vị trí giả
   vào hệ thống.

2. Kỹ thuật viên cài app **"Traccar Client"** từ App Store (iOS) hoặc Google
   Play (Android) — đúng app tên này, có icon hình mũi tên định vị màu xanh.

3. Mở app → bấm vào **biểu tượng bánh răng ⚙️ / "Settings"** ở góc trên (màn
   hình bạn chụp chính là màn hình này). Bạn sẽ thấy các mục sau — điền/chỉnh
   đúng như sau:

   | Mục trong app | Giá trị cần điền | Ghi chú |
   |---|---|---|
   | **Device identifier** | Đúng bằng **username** của kỹ thuật viên đó | Phải khớp *chính xác* (phân biệt hoa/thường) với cột `username` trong Sheet Users, ví dụ `vana`. Bấm vào dòng "17055581" hiện tại để sửa. |
   | **Server URL** : https://ccts-live-tracking.onrender.com/api/traccar?token=%3Copenssl%20rand%20-hex%203893%3E
   | **Location accuracy** | Đổi từ "Medium" → **"High"** | Vì mình đang dùng ngưỡng phát hiện "đang ở tại trạm" rất gần (10 mét), cần độ chính xác GPS cao hơn mức mặc định để nhận diện đúng. |
   | **Distance (meters)** | Đổi từ 75 → **20** (hoặc thấp hơn) | Đây là "cứ di chuyển bao nhiêu mét thì gửi 1 lần cập nhật". Để 75m thì lúc kỹ thuật viên tiến gần vào 1 trạm (bán kính chỉ 10m) có thể bị "nhảy cóc" qua mà server không kịp ghi nhận. |
   | **Stationary heartbeat (seconds)** | Bật lên, đặt **60** (hiện đang "Disabled") | **⚠️ QUAN TRỌNG NHẤT** — xem giải thích riêng ngay bên dưới bảng này. |
   | **Advanced settings** | Không cần bật | Để mặc định là được. |

4. Bấm mũi tên **"<"** quay lại màn hình chính, bấm nút **Continuous tracking**
   (nút lớn ở màn hình chính, không phải trong Settings). Từ lúc này app sẽ tự
   gửi vị trí định kỳ, kể cả khi tắt màn hình hoặc chuyển sang app khác.

> **Vì sao "Stationary heartbeat" quan trọng?** Tính năng "thời gian đang sửa
> trạm" của hệ thống chỉ cập nhật số phút hiển thị mỗi khi có 1 vị trí MỚI gửi
> về. Khi kỹ thuật viên đứng yên tại 1 trạm để sửa chữa, họ **không di
> chuyển** — nếu "Distance" là cách duy nhất để gửi cập nhật, app sẽ NGỪNG gửi
> vị trí ngay khi họ dừng lại, và bộ đếm thời gian trên web sẽ bị "đứng hình"
> tại giá trị lúc họ vừa tới, đồng thời sau 2 phút không có cập nhật, hệ thống
> sẽ hiển thị họ là "offline" (chấm đỏ) dù họ vẫn đang ở đó làm việc. Bật
> "Stationary heartbeat" = 60 giây đảm bảo app vẫn gửi vị trí đều đặn mỗi phút
> ngay cả khi đứng yên, để đồng hồ đếm thời gian sửa trạm chạy đúng và chấm
> online/offline phản ánh đúng thực tế.

Vị trí gửi từ Traccar Client và vị trí gửi từ trình duyệt web đều đi vào cùng
1 hệ thống (`location_hub.py`) — không xung đột, dùng cách nào cũng được, kể
cả dùng cả 2 cùng lúc (server sẽ chỉ giữ lại lần cập nhật mới nhất).

> Không bắt buộc dùng Traccar Client — nếu kỹ thuật viên chỉ mở web trong lúc
> đang thao tác (không cần theo dõi khi màn hình tắt), cách lấy vị trí qua
> trình duyệt hiện tại vẫn hoạt động bình thường.
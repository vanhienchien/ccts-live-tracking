# CCTS Live Map (FastAPI + WebSocket)

Bản thay thế cho ứng dụng Streamlit trước đây — website độc lập, vị trí nhân sự
cập nhật mượt theo thời gian thực (không cần load lại trang), bản đồ trạm sạc
tự làm mới mỗi 10 phút.

## 1. Các file cần bạn tự chép vào (chưa có sẵn ở đây)

Copy nguyên các file sau từ project cũ của bạn vào thư mục này:

- `utils.py`

> `api_client.py` đã có sẵn trong project này (bản API thuần, không dùng
> Playwright). `station_info.json`, `listLongLat.xlsx`, `list_Stations.json`,
> `ChargePoint_Model.xlsx` **không cần copy vào đây nữa** — 3 file
> `listLongLat.xlsx`, `list_Stations.json`, `ChargePoint_Model.xlsx` giờ được
> đọc trực tiếp từ GitHub tại runtime, xem mục 9 bên dưới.

## 2. Cài đặt

```bash
pip install -r requirements.txt
```

## 3. Cấu hình (biến môi trường)

Tạo file `.env` ở thư mục gốc (không commit file này lên Git):

```env
SPREADSHEET_URL=https://docs.google.com/spreadsheets/d/xxxxxxxx/edit
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
CCTS_USERNAME_ES=esmanager
CCTS_PASSWORD=Ccts123.
CCTS_USERNAME_ITS=its_frontdesk 04
CCTS_PASSWORD_its=Duynam123.
TICKET_REFRESH_SECONDS=600

# BẮT BUỘC trên Render — ký cookie web + token mobile (JWT). Thiếu thì mỗi
# lần restart tự sinh khoá ngẫu nhiên mới, đăng xuất hàng loạt. Tạo 1 lần:
# openssl rand -hex 32
SESSION_SECRET_KEY=

# Tuỳ chọn — bật object storage (Cloudflare R2/S3) để cache map + stats
# sống qua redeploy trên Render thay vì chỉ nằm trong git (xem cache_store.py).
CACHE_S3_BUCKET=
CACHE_S3_ACCESS_KEY=
CACHE_S3_SECRET_KEY=
CACHE_S3_ENDPOINT=
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
python -m uvicorn main:app --host 0.0.0.0 --port 8000
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

## 8. Vị trí kỹ thuật viên — nguồn duy nhất là app di động CCTS (Flutter)

Đã bỏ hẳn app Traccar Client và endpoint `/api/traccar` cũ (dùng 1 token tĩnh
dùng chung, ai biết token cũng gọi được). Vị trí giờ CHỈ đến từ app di động
CCTS (Flutter) qua `POST /api/location`, xác thực bằng Bearer token của chính
người đăng nhập (xem `main.py:api_mobile_location`) — an toàn hơn và không
cần quản lý thêm token riêng.

## 9. Toạ độ trạm / phân công kỹ thuật viên / model trụ sạc - đọc trực tiếp từ GitHub

Trước đây 3 file này (`listLongLat.xlsx`, `list_Stations.json`,
`ChargePoint_Model.xlsx`) được đóng gói cùng code khi deploy, hoặc thử
nghiệm qua Google Sheets (chậm hơn nhiều do phải gọi Google Sheets API). Giờ
`github_data_store.py` đọc 3 file này **trực tiếp từ GitHub qua GitHub
Contents API** tại runtime — bạn sửa file trên máy, `git push`, app tự nhận
dữ liệu mới ở lần làm mới kế tiếp (cache 5 phút), **không cần deploy lại**
project trên Render.

**Cách thiết lập:**

1. **Khuyến nghị mạnh: tạo 1 repo GitHub RIÊNG** chỉ chứa 3 file data này,
   KHÔNG kết nối với Render. Đây là điểm quan trọng nhất: nếu 3 file này nằm
   chung repo với code app, mỗi lần bạn push cập nhật data vẫn có thể vô tình
   kích hoạt Render tự động deploy lại (tuỳ cấu hình Auto-Deploy) — tách repo
   riêng loại bỏ hoàn toàn rủi ro này.
2. Đặt biến môi trường trên Render:
   ```env
   GITHUB_DATA_REPO=ten-tai-khoan/ten-repo-data
   GITHUB_DATA_BRANCH=main
   ```
3. Nếu repo đó **public**: không cần thêm gì. Nếu **private**: tạo GitHub
   Personal Access Token (Settings → Developer settings → Fine-grained
   tokens), chỉ cấp quyền **Contents: Read-only** trên đúng repo đó, rồi đặt:
   ```env
   GITHUB_TOKEN=github_pat_xxx...
   ```
4. Nếu tên file khác mặc định, chỉnh thêm (không bắt buộc):
   ```env
   GITHUB_STATION_DATA_CSV_PATH=StationData.csv
   GITHUB_CP_MODEL_JSON_PATH=ChargePointModels.json
   GITHUB_ENGINEER_COORDS_JSON_PATH=EngineerCoords.json
   ```

Từ giờ, quy trình cập nhật của bạn chỉ còn: sửa file trên máy → `git push` →
đợi tối đa 5 phút (hoặc bấm nút 🔄 làm mới thủ công nếu là Admin) → xong,
không đụng gì tới Render.

> Tính năng "chỉnh sửa kỹ thuật viên phụ trách trực tiếp trên bản đồ" đã được
> **gỡ bỏ** theo yêu cầu (đi kèm với việc bỏ Google Sheets cho dữ liệu này) —
> giờ việc đổi kỹ thuật viên phụ trách chỉ thực hiện bằng cách sửa
> `list_Stations.json` rồi push lên GitHub như trên.

## 10. Bản đồ tổng hợp toàn bộ trạm sạc (`/all-stations`)

Trang riêng (`charges_data.py`) hiện **TOÀN BỘ trụ sạc EV + BSS của mọi
trạm trong hệ thống**, kể cả trạm hiện không có sự cố — khác hẳn bản đồ live
(`/`, chỉ hiện trạm ĐANG có ticket mở). Nguồn dữ liệu: `total_charges.xlsx`,
đọc qua GitHub Contents API giống 3 file ở mục 9 (cùng repo, cùng cơ chế
cache 5 phút — sửa file, push, đợi tối đa 5 phút là thấy dữ liệu mới).

Cột bắt buộc trong file Excel (sheet đầu tiên): `SN` (mã trụ — bắt đầu bằng
`BSS-` thì coi là trụ đổi pin, còn lại là trụ EV), `Mã trạm/Code Station`,
`Vĩ độ (lat)`, `Kinh độ (long)`, `Tên trạm/ Name Station`, `Quận Huyện/
District`, `Tỉnh thành/Province`.

Các trụ được **gộp theo TOẠ ĐỘ VẬT LÝ** (làm tròn ~11m), không theo Mã trạm
— vì 1 vị trí thực tế có thể mang 2 Mã trạm khác nhau (1 mã EV, 1 mã BSS).
Trạm có cả 2 loại trụ sẽ ưu tiên hiển thị icon **EV**.

Đổi tên file mặc định (không bắt buộc):
```env
GITHUB_TOTAL_CHARGES_XLSX_PATH=total_charges.xlsx
```
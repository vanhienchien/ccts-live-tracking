import time
import json
from ccts_data import get_static_data, build_station_markers

CACHE_FILE = "stations_cache.json"
REFRESH_INTERVAL = 600  # 10 phút (tính bằng giây)

def run_background_worker():
    print("[*] Khởi động tiến trình cào dữ liệu CCTS độc lập...")
    
    # Nạp dữ liệu tĩnh ban đầu
    try:
        get_static_data()
    except Exception as e:
        print(f"[-] Lỗi nạp dữ liệu tĩnh: {e}")

    while True:
        print(f"\n[+] [{time.strftime('%Y-%m-%d %H:%M:%S')}] Bắt đầu chu kỳ cào dữ liệu mới...")
        try:
            # Gọi hàm cào dữ liệu sử dụng Playwright (chạy hoàn toàn an toàn ở đây)
            payload = build_station_markers()
            
            # Lưu payload ra file JSON trung gian
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=4)
                
            print(f"[✓] Cập nhật cache thành công! Tổng số trạm: {len(payload.get('stations', []))}")
        except Exception as e:
            print(f"[-] Lỗi trong chu kỳ cào dữ liệu: {e}")
        
        print(f"[*] Đang chờ {REFRESH_INTERVAL // 60} phút cho lần cào tiếp theo...")
        time.sleep(REFRESH_INTERVAL)

if __name__ == "__main__":
    run_background_worker()
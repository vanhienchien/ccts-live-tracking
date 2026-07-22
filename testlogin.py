import asyncio
from api_client import CCTSClient

async def test_login():
    # Khởi tạo client với thông tin tài khoản của bạn
    client = CCTSClient(
        username="esmanager", 
        password="Ccts123.", 
        base_url="https://cloud.cnpowercore.com:8091"
    )
    
    try:
        print("=== BẮT ĐẦU TEST ĐĂNG NHẬP ===")
        # Gọi hàm login
        await client.login()
        
        # Kiểm tra điều kiện thành công
        if client.token and client.ssoticket:
            print("\n[TEST KẾT QUẢ: THÀNH CÔNG]")
            print(f" - Lấy được Token: {client.token}")
            print(f" - Lấy được Cookie ssoticket: {client.ssoticket}")
        else:
            print("\n[TEST KẾT QUẢ: THẤT BẠI] Đăng nhập không trả về token hoặc cookie.")
            
    except Exception as e:
        print(f"\n[TEST KẾT QUẢ: LỖI]")
        print(f" - Chi tiết lỗi: {e}")

if __name__ == "__main__":
    # Chạy hàm test bất đồng bộ
    asyncio.run(test_login())
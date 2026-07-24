from static_data_store import load_static_data, reload_static_data
data = load_static_data()   # force reload
print("✅ Hoàn thành. Kết quả:", len(data[0]), "trạm có tọa độ")
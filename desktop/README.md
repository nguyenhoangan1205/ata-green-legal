# FB Auto Poster (bản có tab "Đăng Trang bằng API")

Mã nguồn app desktop FB Auto Poster, thêm tab **🔑 Đăng Trang bằng API**: đăng lên các Trang bạn quản lý
qua Facebook Graph API chính thức, không cần mở Chrome.

## Cập nhật trên máy
Chép đè 3 file này vào `C:\Users\LENOVO\FB Auto Poster`:
- `FB_Auto_Poster.pyw` (đã sửa)
- `fb_graph_api.py` (mới)
- `fb_engine.py` (giữ nguyên, để kèm cho đủ bộ)

Cần thư viện `requests` (một lần): `pip install requests`. Thiếu thì tab API sẽ báo cách cài.

## Cách dùng tab "Đăng Trang bằng API"
1. Bấm **🔗 Mở link lấy token** → Graph API Explorer → chọn app → *Get User Access Token* → tick
   `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` → Generate (chọn các trang) → copy token, dán vào ô.
2. Bấm **🔍 Quét trang bằng API** → danh sách trang hiện ra, tick ☑ các trang muốn đăng.
3. Nội dung, ảnh/video lấy chung từ tab **✍ Đăng bài**. Có thể thêm link kèm bài.
4. Bấm **▶ ĐĂNG BẰNG API LÊN CÁC TRANG ĐÃ CHỌN**. Nút đổi thành **■ DỪNG** khi đang chạy.

Kết quả lưu vào **Lịch sử phiên đăng** (cột Chế độ = "API"); "Chạy lại các nơi lỗi" của phiên API sẽ chạy lại bằng API.
Bấm đúp vào dòng kết quả để mở bài vừa đăng.

Lưu ý:
- Token từ Explorer chỉ sống ~1–2 giờ: bấm **Kiểm tra / gia hạn token** → *Extend Access Token* để dùng ~60 ngày.
- Nhiều ảnh → 1 bài album. Có video → đăng 1 video/bài (API không cho trộn ảnh + video); video tải theo từng phần nên file lớn vẫn được.
- Token (nếu tick "Nhớ token") và danh sách trang lưu trong `data\config.json`.

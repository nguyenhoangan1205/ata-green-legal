# Tab "Đăng Trang bằng API" cho FB Auto Poster

Đăng bài lên các Trang Facebook bạn quản lý qua **Graph API chính thức**, không cần mở Chrome.

## Cài đặt
```
pip install requests
```
Chép `fb_graph_api.py` và `fb_pages_tab.py` vào cùng thư mục với app FB Auto Poster.

## Gắn vào app
Ở chỗ app tạo các tab (`ttk.Notebook` có "Đăng bài", "Lịch sử phiên đăng"), thêm:
```python
from fb_pages_tab import PagesApiTab

api_tab = PagesApiTab(notebook,
    get_content=lambda: (o_noi_dung.get("1.0", "end-1c"), danh_sach_anh_video))
notebook.add(api_tab, text="🔑 Đăng Trang bằng API")
```
`get_content` là tuỳ chọn: thay `o_noi_dung` / `danh_sach_anh_video` bằng tên biến thật trong app để
nút "Lấy nội dung từ tab Đăng bài" lấy được bài đang soạn. Chạy thử riêng: `python fb_pages_tab.py`.

## Cách dùng
1. Bấm **🔗 Mở link lấy token** → Graph API Explorer → chọn app → *Get User Access Token* →
   tick `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` → Generate (chọn các trang) → copy token.
2. Dán token, bấm **🔍 Quét trang bằng API** → hiện danh sách trang.
3. Soạn nội dung / thêm ảnh, video → tick trang → **▶ ĐĂNG BẰNG API**.

Lưu ý: token từ Explorer chỉ sống ~1–2 giờ. Dùng nút **Kiểm tra / gia hạn token**
(Access Token Debugger → *Extend Access Token*) để có token ~60 ngày.
Token được lưu (nếu tick "Nhớ token") tại `%USERPROFILE%\.fb_auto_poster_api.json`.

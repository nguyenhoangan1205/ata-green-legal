"""Tab "Đăng Trang bằng API" cho FB Auto Poster.

Gắn vào app sẵn có (ttk.Notebook):

    from fb_pages_tab import PagesApiTab
    tab = PagesApiTab(notebook, get_content=lambda: (text_widget.get("1.0", "end-1c"), media_paths))
    notebook.add(tab, text="🔑 Đăng Trang bằng API")

`get_content` là tuỳ chọn: hàm trả về (nội_dung, [đường_dẫn_ảnh_video]) của tab
"Đăng bài" để lấy sang. Chạy thử riêng: python fb_pages_tab.py
"""
import json
import os
import random
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import fb_graph_api as api

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".fb_auto_poster_api.json")
CHECKED, UNCHECKED = "☑", "☐"


def _load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


class PagesApiTab(ttk.Frame):
    def __init__(self, master, get_content=None, **kw):
        super().__init__(master, padding=8, **kw)
        self.get_content = get_content
        self.pages = {}          # iid -> page dict (có access_token)
        self.media = []
        self.running = False
        self.stop_flag = threading.Event()
        cfg = _load_config()

        # ---- 1. Token ----
        box = ttk.LabelFrame(self, text="1. Access Token Facebook (User token)", padding=6)
        box.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(box, text="🔗 Mở link lấy token", command=self.open_token_page).grid(row=0, column=0)
        ttk.Button(box, text="🔎 Kiểm tra / gia hạn token",
                   command=lambda: webbrowser.open(api.DEBUG_TOKEN_URL)).grid(row=0, column=1, padx=4)
        self.token_var = tk.StringVar(value=cfg.get("user_token", ""))
        self.token_entry = ttk.Entry(box, textvariable=self.token_var, show="•")
        self.token_entry.grid(row=0, column=2, sticky="ew", padx=4)
        self.show_var = tk.BooleanVar()
        ttk.Checkbutton(box, text="Hiện", variable=self.show_var,
                        command=lambda: self.token_entry.config(show="" if self.show_var.get() else "•")
                        ).grid(row=0, column=3)
        self.remember_var = tk.BooleanVar(value=bool(cfg.get("user_token")))
        ttk.Checkbutton(box, text="Nhớ token", variable=self.remember_var).grid(row=0, column=4, padx=4)
        ttk.Button(box, text="🔍 Quét trang bằng API", command=self.scan_pages).grid(row=0, column=5)
        box.columnconfigure(2, weight=1)
        ttk.Label(box, foreground="#6b7280", text=(
            "Trong Graph API Explorer: chọn app → 'Get User Access Token' → tick quyền "
            + ", ".join(api.REQUIRED_SCOPES) + " → Generate → copy token dán vào ô trên.")
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

        # ---- 2. Nội dung ----
        left = ttk.LabelFrame(self, text="2. Nội dung đăng", padding=6)
        left.grid(row=1, column=0, sticky="nsew", pady=6, padx=(0, 6))
        if get_content:
            ttk.Button(left, text="⇩ Lấy nội dung + ảnh/video từ tab Đăng bài",
                       command=self.pull_content).pack(anchor="w")
        self.text = tk.Text(left, height=8, wrap="word", font=("Segoe UI", 11))
        self.text.pack(fill="both", expand=True, pady=4)
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Label(row, text="Link (tuỳ chọn):").pack(side="left")
        self.link_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.link_var).pack(side="left", fill="x", expand=True, padx=4)
        self.media_list = tk.Listbox(left, height=4)
        self.media_list.pack(fill="both", expand=True, pady=4)
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Button(row, text="+ Thêm ảnh / video", command=self.add_media).pack(side="left")
        ttk.Button(row, text="Xoá", command=self.remove_media).pack(side="left", padx=4)
        ttk.Label(row, foreground="#6b7280",
                  text="Nhiều ảnh = 1 bài album. Có video: đăng 1 video/bài.").pack(side="left", padx=6)

        # ---- 3. Trang ----
        right = ttk.LabelFrame(self, text="3. Trang tôi quản lý (bấm ô ☐ để chọn)", padding=6)
        right.grid(row=1, column=1, sticky="nsew", pady=6)
        row = ttk.Frame(right)
        row.pack(fill="x")
        ttk.Button(row, text="☑ Chọn tất cả", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(row, text="☐ Bỏ chọn tất cả", command=lambda: self.set_all(False)).pack(side="left", padx=4)
        self.account_lbl = ttk.Label(row, foreground="#15803d")
        self.account_lbl.pack(side="right")
        self.tree = ttk.Treeview(right, columns=("chk", "name", "id", "link"), show="headings", height=8)
        for col, title, width in (("chk", "✔", 40), ("name", "Tên trang", 220),
                                  ("id", "Page ID", 140), ("link", "Link", 220)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="center" if col == "chk" else "w")
        self.tree.pack(fill="both", expand=True, pady=4)
        self.tree.bind("<Button-1>", self.on_tree_click)

        # ---- 4. Đăng + kết quả ----
        bottom = ttk.LabelFrame(self, text="4. Đăng bằng API", padding=6)
        bottom.grid(row=2, column=0, columnspan=2, sticky="nsew")
        row = ttk.Frame(bottom)
        row.pack(fill="x")
        self.post_btn = tk.Button(row, text="▶  ĐĂNG BẰNG API LÊN CÁC TRANG ĐÃ CHỌN", bg="#1877f2",
                                  fg="white", font=("Segoe UI", 12, "bold"), relief="flat",
                                  padx=14, pady=6, command=self.start_posting)
        self.post_btn.pack(side="left")
        ttk.Button(row, text="■ Dừng", command=self.stop_flag.set).pack(side="left", padx=6)
        ttk.Label(row, text="Nghỉ giữa 2 trang từ").pack(side="left", padx=(12, 2))
        self.delay_min = tk.IntVar(value=cfg.get("delay_min", 10))
        self.delay_max = tk.IntVar(value=cfg.get("delay_max", 20))
        ttk.Spinbox(row, from_=0, to=3600, width=5, textvariable=self.delay_min).pack(side="left")
        ttk.Label(row, text="đến").pack(side="left", padx=2)
        ttk.Spinbox(row, from_=0, to=3600, width=5, textvariable=self.delay_max).pack(side="left")
        ttk.Label(row, text="giây").pack(side="left", padx=2)
        self.status = ttk.Label(row, font=("Segoe UI", 11, "bold"))
        self.status.pack(side="right")
        self.result = ttk.Treeview(bottom, columns=("time", "page", "state", "note"), show="headings", height=6)
        for col, title, width in (("time", "Giờ", 70), ("page", "Trang", 220),
                                  ("state", "Kết quả", 90), ("note", "Ghi chú / Link bài", 520)):
            self.result.heading(col, text=title)
            self.result.column(col, width=width)
        self.result.pack(fill="both", expand=True, pady=4)
        self.result.bind("<Double-1>", self.open_result_link)

        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

    # ---------- helpers ----------
    def ui(self, fn, *args):
        """Gọi hàm giao diện từ luồng nền một cách an toàn."""
        self.after(0, lambda: fn(*args))

    def set_status(self, text, color="#111827"):
        self.status.config(text=text, foreground=color)

    def token(self):
        return self.token_var.get().strip()

    def open_token_page(self):
        webbrowser.open(api.TOKEN_URL)

    def remember(self):
        cfg = _load_config()
        cfg["user_token"] = self.token() if self.remember_var.get() else ""
        cfg["delay_min"], cfg["delay_max"] = self.delay_range()
        _save_config(cfg)

    def delay_range(self):
        try:
            lo, hi = int(self.delay_min.get()), int(self.delay_max.get())
        except (tk.TclError, ValueError):
            lo, hi = 10, 20
        lo = max(lo, 0)
        return lo, max(hi, lo)

    # ---------- nội dung ----------
    def pull_content(self):
        try:
            message, media = self.get_content()
        except Exception as e:  # app chính có thể chưa có nội dung
            messagebox.showerror("Lỗi", f"Không lấy được nội dung: {e}")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", message or "")
        self.media = list(media or [])
        self.refresh_media()

    def add_media(self):
        exts = " ".join("*" + e for e in sorted(api.PHOTO_EXT | api.VIDEO_EXT))
        paths = filedialog.askopenfilenames(title="Chọn ảnh / video",
                                            filetypes=[("Ảnh / video", exts), ("Tất cả", "*.*")])
        self.media.extend(p for p in paths if p not in self.media)
        self.refresh_media()

    def remove_media(self):
        for i in reversed(self.media_list.curselection()):
            del self.media[i]
        self.refresh_media()

    def refresh_media(self):
        self.media_list.delete(0, "end")
        for p in self.media:
            size = os.path.getsize(p) / 1048576 if os.path.exists(p) else 0
            icon = "🎬" if os.path.splitext(p)[1].lower() in api.VIDEO_EXT else "🖼"
            self.media_list.insert("end", f"{icon} {os.path.basename(p)}  ({size:.1f} MB)")

    # ---------- quét trang ----------
    def scan_pages(self):
        token = self.token()
        if not token:
            if messagebox.askyesno("Chưa có token", "Chưa có token. Mở trang lấy token ngay?"):
                self.open_token_page()
            return
        self.remember()
        self.set_status("Đang quét trang...", "#2563eb")

        def work():
            try:
                me = api.get_me(token)
                pages = api.list_pages(token)
            except Exception as e:
                self.ui(self.set_status, "Quét lỗi", "#dc2626")
                self.ui(messagebox.showerror, "Quét trang lỗi",
                        f"{e}\n\nToken có thể đã hết hạn hoặc thiếu quyền: {', '.join(api.REQUIRED_SCOPES)}")
                return
            self.ui(self.show_pages, me, pages)

        threading.Thread(target=work, daemon=True).start()

    def show_pages(self, me, pages):
        self.tree.delete(*self.tree.get_children())
        self.pages.clear()
        for p in pages:
            iid = self.tree.insert("", "end", values=(UNCHECKED, p["name"], p["id"], p["link"]))
            self.pages[iid] = p
        self.account_lbl.config(text=f"Token của: {me.get('name', '')}")
        self.set_status(f"Tìm thấy {len(pages)} trang", "#15803d")
        if not pages:
            messagebox.showinfo("Không có trang", "Token này không quản lý trang nào, hoặc chưa được "
                                "cấp quyền cho trang (khi Generate token nhớ chọn các trang).")

    def on_tree_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and self.tree.identify_column(event.x) == "#1":
            self.toggle(iid)

    def toggle(self, iid, value=None):
        vals = list(self.tree.item(iid, "values"))
        checked = (vals[0] != CHECKED) if value is None else value
        vals[0] = CHECKED if checked else UNCHECKED
        self.tree.item(iid, values=vals)

    def set_all(self, value):
        for iid in self.tree.get_children():
            self.toggle(iid, value)

    def selected_pages(self):
        return [self.pages[i] for i in self.tree.get_children()
                if self.tree.item(i, "values")[0] == CHECKED]

    # ---------- đăng ----------
    def start_posting(self):
        if self.running:
            return
        pages = self.selected_pages()
        message = self.text.get("1.0", "end-1c").strip()
        link = self.link_var.get().strip()
        media = list(self.media)
        if not pages:
            messagebox.showwarning("Chưa chọn trang", "Hãy quét và tick chọn ít nhất 1 trang.")
            return
        if not (message or link or media):
            messagebox.showwarning("Trống", "Chưa có nội dung, link hay ảnh/video.")
            return
        missing = [p for p in media if not os.path.exists(p)]
        if missing:
            messagebox.showerror("Thiếu tệp", "Không tìm thấy:\n" + "\n".join(missing))
            return
        if not messagebox.askyesno("Xác nhận", f"Đăng bài lên {len(pages)} trang bằng API?"):
            return
        self.remember()
        lo, hi = self.delay_range()
        self.running = True
        self.stop_flag.clear()
        self.post_btn.config(state="disabled")
        self.result.delete(*self.result.get_children())
        threading.Thread(target=self.post_worker, args=(pages, message, media, link, lo, hi),
                         daemon=True).start()

    def post_worker(self, pages, message, media, link, lo, hi):
        ok = 0
        for i, page in enumerate(pages, 1):
            if self.stop_flag.is_set():
                break
            self.ui(self.set_status, f"Đang đăng {i}/{len(pages)}: {page['name']}", "#2563eb")
            try:
                post_id, note = api.post_to_page(page, message, media, link)
                url = f"https://facebook.com/{post_id}" if post_id else ""
                self.ui(self.add_result, page["name"], "✅ Thành công", " — ".join(x for x in (url, note) if x))
                ok += 1
            except Exception as e:
                self.ui(self.add_result, page["name"], "❌ Lỗi", str(e))
            if i < len(pages):
                wait = random.randint(lo, hi)
                for _ in range(wait):
                    if self.stop_flag.is_set():
                        break
                    time.sleep(1)
        stopped = self.stop_flag.is_set()
        self.ui(self.finish, ok, len(pages), stopped)

    def add_result(self, page, state, note):
        self.result.insert("", "end", values=(datetime.now().strftime("%H:%M:%S"), page, state, note))

    def finish(self, ok, total, stopped):
        self.running = False
        self.post_btn.config(state="normal")
        text = f"{'Đã dừng' if stopped else 'Hoàn tất'}: {ok}/{total} trang thành công"
        self.set_status(text, "#15803d" if ok == total else "#dc2626")

    def open_result_link(self, event):
        iid = self.result.identify_row(event.y)
        if iid:
            note = self.result.item(iid, "values")[3]
            if note.startswith("https://"):
                webbrowser.open(note.split(" ")[0])


if __name__ == "__main__":
    root = tk.Tk()
    root.title("FB Auto Poster – Đăng Trang bằng API")
    root.geometry("1200x800")
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    nb.add(PagesApiTab(nb), text="🔑 Đăng Trang bằng API")
    root.mainloop()

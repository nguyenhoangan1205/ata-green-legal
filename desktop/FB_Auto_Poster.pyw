"""FB Auto Poster — ứng dụng desktop đăng bài lên nhiều Group/Trang Facebook.
Mở bằng file "Mo FB Auto Poster.bat" (hoặc nhấp đúp file này)."""
import json
import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
CONFIG = os.path.join(DATA_DIR, "config.json")
HISTORY = os.path.join(DATA_DIR, "history.json")
os.makedirs(DATA_DIR, exist_ok=True)
sys.path.insert(0, APP_DIR)

try:
    from fb_engine import Engine, is_video
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror("FB Auto Poster", "Chưa cài thư viện. Hãy chạy file 'Cai dat lan dau.bat' trước.")
    sys.exit(1)

try:
    import fb_graph_api as fbapi
except ImportError:   # thiếu thư viện requests -> tab API báo cách cài
    fbapi = None

ICON = {"queue": "⏸", "run": "⏳", "posted": "✅", "pending": "🕓", "skipped": "⏭", "error": "❌", "stopped": "■"}
RETRY_STATES = ("error", "skipped", "stopped", "queue", "run")
URL_RE = re.compile(r"https://\S+")
ON, OFF = "☑", "☐"


def norm_url(u):
    u = u.strip()
    if not re.match(r"^https?://([a-z]+\.)?facebook\.com/", u, re.I):
        return None
    u = re.sub(r"^https?://([a-z]+\.)?facebook\.com", "https://www.facebook.com", u, flags=re.I)
    return u.split("#")[0].rstrip("/")


NAME_PREFIX = re.compile(r"^(ảnh đại diện của|profile picture of|trang cá nhân của)\s*", re.I)


def clean_name(n):
    return NAME_PREFIX.sub("", (n or "").strip())


def kind_of(url):
    return "Group" if "/groups/" in url else "Trang"


def short(url):
    return url.replace("https://www.facebook.com", "")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class App:
    def __init__(self, root):
        self.root = root
        root.title("FB Auto Poster")
        root.geometry("1180x760")
        root.minsize(980, 560)
        try:
            root.state("zoomed")   # mở toàn màn hình
        except tk.TclError:
            pass
        self.media = []
        self.targets = []      # [{url, name, checked}]  — danh sách nơi đăng (lưu lại)
        self.history = load_json(HISTORY, [])   # các phiên đăng, mới nhất ở cuối
        self.session = None    # phiên đang chạy / vừa chạy
        self.busy = False
        self.api_pages = []    # [{id, name, link, checked}]  — Trang quét bằng Graph API (lưu lại)
        self.api_events = queue.Queue()
        self.api_stop = threading.Event()
        self.engine = Engine(os.path.join(os.environ.get("LOCALAPPDATA", DATA_DIR), "FBAutoPoster", "browser-profile-chrome"))
        self.engine.start()
        self.build()
        self.load()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(200, self.poll)

    # =============================== GIAO DIỆN ===============================
    def build(self):
        st = ttk.Style()
        try:
            st.theme_use("vista")
        except tk.TclError:
            pass
        st.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        st.configure("Treeview", rowheight=24)
        self.root.option_add("*Font", ("Segoe UI", 10))

        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="📣 FB Auto Poster", font=("Segoe UI", 15, "bold")).pack(side="left")
        ttk.Button(top, text="🌐 Mở Chrome / Đăng nhập Facebook", command=self.open_browser).pack(side="right")
        self.who = ttk.Label(top, text="Đang dùng: (chưa kiểm tra — bấm 'Mở Chrome')", foreground="#65676b",
                             font=("Segoe UI", 10, "bold"))
        self.who.pack(side="right", padx=12)

        # Thanh nút chính đặt ngay dưới tiêu đề để luôn nhìn thấy
        bar = tk.Frame(self.root, bg="#e7f0fd", padx=10, pady=8)
        bar.pack(fill="x", padx=10, pady=(0, 8))
        big = dict(font=("Segoe UI", 12, "bold"), relief="flat", cursor="hand2", padx=18, pady=6, bd=0,
                   disabledforeground="#e4e6eb")
        # 1 nút duy nhất: bấm để Đăng, khi đang chạy nút đổi thành DỪNG
        self.start_btn = tk.Button(bar, command=self.toggle_run, width=30, **big)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.paint_main_btn(False)
        self.retry_btn = ttk.Button(bar, text="↻ Chạy lại các nơi lỗi", command=self.retry_current, state="disabled")
        self.retry_btn.pack(side="left", ipady=4)
        self.status = tk.Label(bar, text="Sẵn sàng", font=("Segoe UI", 11, "bold"), bg="#e7f0fd", fg="#1c1e21")
        self.status.pack(side="right")

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10)
        post_tab = ttk.Frame(self.nb, padding=8)
        api_tab = ttk.Frame(self.nb, padding=8)
        hist_tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(post_tab, text="  ✍  Đăng bài  ")
        self.nb.add(api_tab, text="  🔑  Đăng Trang bằng API  ")
        self.nb.add(hist_tab, text="  🕘  Lịch sử phiên đăng  ")
        self.api_tab, self.hist_tab = str(api_tab), str(hist_tab)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.on_tab_changed())
        self.build_post_tab(post_tab)
        self.build_api_tab(api_tab)
        self.build_history_tab(hist_tab)
        self.paint_main_btn(False)
        ttk.Frame(self.root, height=8).pack(fill="x")

    def build_post_tab(self, tab):
        tab.columnconfigure(0, weight=5, uniform="c")
        tab.columnconfigure(1, weight=6, uniform="c")
        tab.rowconfigure(0, weight=1)

        # ---- Cột trái: nội dung + media ----
        left = ttk.Frame(tab)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=3)
        left.rowconfigure(1, weight=2)
        left.columnconfigure(0, weight=1)

        f = ttk.LabelFrame(left, text=" Nội dung bài đăng ", padding=6)
        f.grid(row=0, column=0, sticky="nsew")
        self.text = tk.Text(f, wrap="word", height=10, undo=True, relief="solid", borderwidth=1)
        self.text.pack(fill="both", expand=True)
        self.chars = ttk.Label(f, text="0 ký tự", foreground="#65676b")
        self.chars.pack(anchor="w")
        self.text.bind("<KeyRelease>", lambda e: self.chars.config(text=f"{len(self.get_text())} ký tự"))

        f = ttk.LabelFrame(left, text=" Ảnh & video ", padding=6)
        f.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.mlist = tk.Listbox(f, height=6, relief="solid", borderwidth=1, selectmode="extended")
        self.mlist.pack(fill="both", expand=True)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="＋ Thêm ảnh / video", command=self.add_media).pack(side="left")
        ttk.Button(bar, text="Xoá", command=self.del_media).pack(side="left", padx=4)
        ttk.Button(bar, text="▲", width=3, command=lambda: self.move_media(-1)).pack(side="left")
        ttk.Button(bar, text="▼", width=3, command=lambda: self.move_media(1)).pack(side="left", padx=4)
        self.minfo = ttk.Label(bar, foreground="#65676b")
        self.minfo.pack(side="right")

        # ---- Cột phải: nơi đăng + cài đặt + kết quả ----
        right = ttk.Frame(tab)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=3)
        right.rowconfigure(2, weight=2)
        right.columnconfigure(0, weight=1)

        f = ttk.LabelFrame(right, text=" Nơi đăng (tick ☑ để chọn) ", padding=6)
        f.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.scan_btns = [
            ttk.Button(bar, text="🔍 Quét group đã tham gia", command=lambda: self.scan("groups")),
            ttk.Button(bar, text="🔍 Quét trang tôi quản lý", command=lambda: self.scan("pages")),
            ttk.Button(bar, text="＋ Thêm link", command=self.add_links),
        ]
        for i, b in enumerate(self.scan_btns):
            b.pack(side="left", padx=(0 if i == 0 else 6, 0))

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="☑ Chọn tất cả", command=lambda: self.check_all(True)).pack(side="left")
        ttk.Button(bar, text="☐ Bỏ chọn tất cả", command=lambda: self.check_all(False)).pack(side="left", padx=6)
        ttk.Button(bar, text="🗑 Xoá dòng", command=self.delete_targets).pack(side="left")
        self.kind_filter = tk.StringVar(value="Tất cả")
        cb = ttk.Combobox(bar, textvariable=self.kind_filter, values=("Tất cả", "Group", "Trang"), width=7, state="readonly")
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda e: self.render_targets())
        self.tfilter = tk.StringVar()
        ttk.Entry(bar, textvariable=self.tfilter, width=16).pack(side="right", padx=4)
        ttk.Label(bar, text="Lọc:").pack(side="right")
        self.tfilter.trace_add("write", lambda *_: self.render_targets())

        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        self.ttree = ttk.Treeview(wrap, columns=("c", "k", "name", "url"), show="headings", selectmode="extended")
        for c, t, w in (("c", "✔", 36), ("k", "Loại", 60), ("name", "Tên", 230), ("url", "Link", 200)):
            self.ttree.heading(c, text=t)
            self.ttree.column(c, width=w, stretch=c in ("name", "url"), anchor="center" if c in ("c", "k") else "w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.ttree.yview)
        self.ttree.configure(yscrollcommand=sb.set)
        self.ttree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.ttree.bind("<Button-1>", self.on_target_click)
        self.ttree.bind("<space>", lambda e: self.toggle_selected())
        self.tcount = ttk.Label(f, foreground="#65676b",
                                text="")
        self.tcount.pack(anchor="w")

        f = ttk.LabelFrame(right, text=" Cài đặt ", padding=6)
        f.grid(row=1, column=0, sticky="ew", pady=8)
        ttk.Label(f, text="Nghỉ giữa 2 bài từ").pack(side="left")
        self.dmin = tk.IntVar(value=20)
        self.dmax = tk.IntVar(value=20)
        ttk.Spinbox(f, from_=10, to=3600, width=6, textvariable=self.dmin).pack(side="left", padx=4)
        ttk.Label(f, text="đến").pack(side="left")
        ttk.Spinbox(f, from_=10, to=3600, width=6, textvariable=self.dmax).pack(side="left", padx=4)
        ttk.Label(f, text="giây").pack(side="left")
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Tự bấm Đăng", variable=self.auto).pack(side="right")

        f = ttk.LabelFrame(right, text=" Kết quả phiên hiện tại ", padding=6)
        f.grid(row=2, column=0, sticky="nsew")
        self.rtree = self.make_result_tree(f)
        self.summary = ttk.Label(f, foreground="#65676b")
        self.summary.pack(anchor="w")

    def make_result_tree(self, parent):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=("st", "name", "msg"), show="headings", height=6)
        for c, t, w in (("st", "", 34), ("name", "Nơi đăng", 240), ("msg", "Ghi chú", 240)):
            tree.heading(c, text=t)
            tree.column(c, width=w, stretch=c != "st", anchor="center" if c == "st" else "w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tree

    def build_api_tab(self, tab):
        tab.columnconfigure(0, weight=6, uniform="a")
        tab.columnconfigure(1, weight=5, uniform="a")
        tab.rowconfigure(1, weight=1)

        # ---- 1. Token ----
        f = ttk.LabelFrame(tab, text=" 1. Access token Facebook (User token) ", padding=6)
        f.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        f.columnconfigure(2, weight=1)
        ttk.Button(f, text="🔗 Mở link lấy token", command=lambda: webbrowser.open(fbapi.TOKEN_URL if fbapi else
                   "https://developers.facebook.com/tools/explorer/")).grid(row=0, column=0)
        ttk.Button(f, text="🔎 Kiểm tra / gia hạn token", command=lambda: webbrowser.open(
            "https://developers.facebook.com/tools/debug/accesstoken/")).grid(row=0, column=1, padx=6)
        self.api_token = tk.StringVar()
        self.api_token_entry = ttk.Entry(f, textvariable=self.api_token, show="•")
        self.api_token_entry.grid(row=0, column=2, sticky="ew")
        self.api_show = tk.BooleanVar()
        ttk.Checkbutton(f, text="Hiện", variable=self.api_show, command=lambda: self.api_token_entry.config(
            show="" if self.api_show.get() else "•")).grid(row=0, column=3, padx=(6, 0))
        self.api_remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Nhớ token", variable=self.api_remember).grid(row=0, column=4, padx=6)
        self.api_scan_btn = ttk.Button(f, text="🔍 Quét trang bằng API", command=self.api_scan)
        self.api_scan_btn.grid(row=0, column=5)
        ttk.Label(f, foreground="#65676b", text=(
            "Cách lấy token: bấm 'Mở link lấy token' → chọn app → Get User Access Token → tick quyền "
            "pages_show_list, pages_read_engagement, pages_manage_posts → Generate (chọn các trang) → copy dán vào ô trên."
            "\nToken từ Explorer chỉ sống ~1–2 giờ: bấm 'Kiểm tra / gia hạn token' → Extend Access Token để dùng ~60 ngày.")
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

        # ---- 2. Danh sách Trang ----
        f = ttk.LabelFrame(tab, text=" 2. Trang tôi quản lý (tick ☑ để chọn) ", padding=6)
        f.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="☑ Chọn tất cả", command=lambda: self.api_check_all(True)).pack(side="left")
        ttk.Button(bar, text="☐ Bỏ chọn tất cả", command=lambda: self.api_check_all(False)).pack(side="left", padx=6)
        self.api_who = ttk.Label(bar, foreground="#31a24c", font=("Segoe UI", 10, "bold"))
        self.api_who.pack(side="right")
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.api_ptree = ttk.Treeview(wrap, columns=("c", "name", "id", "url"), show="headings", selectmode="extended")
        for c, t, w in (("c", "✔", 36), ("name", "Tên trang", 230), ("id", "Page ID", 140), ("url", "Link", 180)):
            self.api_ptree.heading(c, text=t)
            self.api_ptree.column(c, width=w, stretch=c in ("name", "url"), anchor="center" if c == "c" else "w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.api_ptree.yview)
        self.api_ptree.configure(yscrollcommand=sb.set)
        self.api_ptree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.api_ptree.bind("<Button-1>", self.on_api_page_click)
        self.api_ptree.bind("<space>", lambda e: self.api_toggle_selected())
        self.api_pcount = ttk.Label(f, foreground="#65676b")
        self.api_pcount.pack(anchor="w")

        # ---- 3. Nội dung + đăng ----
        right = ttk.Frame(tab)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        f = ttk.LabelFrame(right, text=" 3. Nội dung & đăng ", padding=6)
        f.grid(row=0, column=0, sticky="ew")
        self.api_content = ttk.Label(f, foreground="#1c1e21", wraplength=480, justify="left")
        self.api_content.pack(anchor="w")
        ttk.Button(f, text="✍ Sửa nội dung / ảnh / video (tab Đăng bài)", command=lambda: self.nb.select(0)
                   ).pack(anchor="w", pady=(4, 6))
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Link kèm bài (tuỳ chọn):").pack(side="left")
        self.api_link = tk.StringVar()
        ttk.Entry(row, textvariable=self.api_link).pack(side="left", fill="x", expand=True, padx=4)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        ttk.Label(row, text="Nghỉ giữa 2 trang từ").pack(side="left")
        ttk.Spinbox(row, from_=10, to=3600, width=6, textvariable=self.dmin).pack(side="left", padx=4)
        ttk.Label(row, text="đến").pack(side="left")
        ttk.Spinbox(row, from_=10, to=3600, width=6, textvariable=self.dmax).pack(side="left", padx=4)
        ttk.Label(row, text="giây").pack(side="left")
        self.api_btn = tk.Button(f, command=self.toggle_api_run, font=("Segoe UI", 12, "bold"), relief="flat",
                                 cursor="hand2", padx=18, pady=6, bd=0, disabledforeground="#e4e6eb")
        self.api_btn.pack(fill="x")
        ttk.Label(f, foreground="#65676b", wraplength=480, justify="left", text=(
            "Nhiều ảnh → 1 bài album. Có video → đăng 1 video/bài (API không cho trộn ảnh + video)."
        )).pack(anchor="w", pady=(4, 0))

        f = ttk.LabelFrame(right, text=" Kết quả (bấm đúp để mở bài) ", padding=6)
        f.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.api_rtree = self.make_result_tree(f)
        self.api_rtree.bind("<Double-1>", self.open_result_link)
        self.api_summary = ttk.Label(f, foreground="#65676b")
        self.api_summary.pack(anchor="w")

    def build_history_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=2)
        tab.rowconfigure(2, weight=3)

        wrap = ttk.Frame(tab)
        wrap.grid(row=0, column=0, sticky="nsew")
        self.htree = ttk.Treeview(wrap, columns=("t", "txt", "n", "ok", "pend", "err", "mode"), show="headings", selectmode="browse")
        for c, t, w in (("t", "Thời gian", 140), ("txt", "Nội dung", 380), ("n", "Số nơi", 60), ("ok", "✅", 50),
                        ("pend", "🕓", 50), ("err", "❌/⏭", 60), ("mode", "Chế độ", 90)):
            self.htree.heading(c, text=t)
            self.htree.column(c, width=w, stretch=c == "txt", anchor="w" if c == "txt" else "center")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.htree.yview)
        self.htree.configure(yscrollcommand=sb.set)
        self.htree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.htree.bind("<<TreeviewSelect>>", lambda e: self.show_session_detail())

        bar = ttk.Frame(tab)
        bar.grid(row=1, column=0, sticky="ew", pady=8)
        self.hbtns = [
            ttk.Button(bar, text="📥 Nạp nội dung + ảnh/video về trang Đăng bài", command=self.load_session_content),
            ttk.Button(bar, text="↻ Chạy lại các nơi lỗi của phiên này", command=self.retry_session),
            ttk.Button(bar, text="🗑 Xoá phiên", command=self.delete_session),
        ]
        for b in self.hbtns:
            b.pack(side="left", padx=(0, 6))

        detail = ttk.Frame(tab)
        detail.grid(row=2, column=0, sticky="nsew")
        detail.columnconfigure(0, weight=2)
        detail.columnconfigure(1, weight=3)
        detail.rowconfigure(0, weight=1)
        f = ttk.LabelFrame(detail, text=" Nội dung đã đăng ", padding=6)
        f.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.htext = tk.Text(f, wrap="word", height=8, relief="solid", borderwidth=1, state="disabled")
        self.htext.pack(fill="both", expand=True)
        self.hmedia = ttk.Label(f, foreground="#65676b", wraplength=380, justify="left")
        self.hmedia.pack(anchor="w")
        f = ttk.LabelFrame(detail, text=" Kết quả từng nơi ", padding=6)
        f.grid(row=0, column=1, sticky="nsew")
        self.hrtree = self.make_result_tree(f)

    # =============================== DỮ LIỆU ===============================
    def get_text(self):
        return self.text.get("1.0", "end-1c")

    def save(self):
        cfg = {"text": self.get_text(), "media": self.media, "targets": self.targets,
               "dmin": self.safe_int(self.dmin, 20), "dmax": self.safe_int(self.dmax, 20), "auto": self.auto.get(),
               "api_token": self.api_token.get().strip() if self.api_remember.get() else "",
               "api_remember": self.api_remember.get(), "api_link": self.api_link.get().strip(),
               "api_pages": self.api_pages}
        save_json(CONFIG, cfg)

    def save_history(self):
        save_json(HISTORY, self.history[-300:])

    def load(self):
        cfg = load_json(CONFIG, {})
        self.text.insert("1.0", cfg.get("text", ""))
        self.media = cfg.get("media", [])
        self.targets = cfg.get("targets", [])
        for t in self.targets:
            t["name"] = clean_name(t.get("name"))
        # Chuyển từ bản cũ (ô dán link) sang danh sách có tick
        for line in (cfg.get("urls") or "").splitlines():
            parts = line.strip().split(None, 1)
            if parts and not parts[0].startswith("#"):
                self.add_target(parts[0], parts[1] if len(parts) > 1 else "", True)
        self.dmin.set(cfg.get("dmin", 20))
        self.dmax.set(cfg.get("dmax", 20))
        self.auto.set(cfg.get("auto", False))
        self.api_token.set(cfg.get("api_token", ""))
        self.api_remember.set(cfg.get("api_remember", True))
        self.api_link.set(cfg.get("api_link", ""))
        self.api_pages = cfg.get("api_pages", [])
        self.render_api_pages()
        self.chars.config(text=f"{len(self.get_text())} ký tự")
        self.render_media()
        self.render_targets()
        # phiên dang dở do tắt app giữa chừng
        for s in self.history:
            if not s.get("end"):
                s["end"] = s.get("start")
                for r in s["results"]:
                    if r["status"] in ("queue", "run"):
                        r.update(status="stopped", msg="App bị tắt khi đang chạy")
        self.render_history()

    @staticmethod
    def safe_int(var, default):
        try:
            return int(var.get())
        except Exception:
            return default

    # ---------- Ảnh / video ----------
    def render_media(self):
        self.mlist.delete(0, "end")
        total, nv = 0, 0
        for m in self.media:
            ok = os.path.exists(m)
            size = os.path.getsize(m) if ok else 0
            total += size
            nv += is_video(m)
            tag = "🎬" if is_video(m) else "🖼"
            self.mlist.insert("end", f"{tag}  {os.path.basename(m)}   ({size / 1048576:.1f} MB)" if ok
                              else f"⚠  {os.path.basename(m)}  (không tìm thấy file)")
        self.minfo.config(text=f"{len(self.media) - nv} ảnh · {nv} video · {total / 1048576:.1f} MB" if self.media else "")

    def add_media(self):
        paths = filedialog.askopenfilenames(title="Chọn ảnh / video", filetypes=[
            ("Ảnh & video", "*.jpg *.jpeg *.png *.gif *.webp *.heic *.mp4 *.mov *.m4v *.avi *.mkv *.webm *.wmv *.3gp"),
            ("Tất cả", "*.*")])
        for p in paths:
            p = os.path.normpath(p)
            if p not in self.media:
                self.media.append(p)
        self.render_media()
        self.save()

    def del_media(self):
        for i in sorted(self.mlist.curselection(), reverse=True):
            del self.media[i]
        self.render_media()
        self.save()

    def move_media(self, d):
        sel = self.mlist.curselection()
        if len(sel) != 1:
            return
        i, j = sel[0], sel[0] + d
        if 0 <= j < len(self.media):
            self.media[i], self.media[j] = self.media[j], self.media[i]
            self.render_media()
            self.mlist.selection_set(j)
            self.save()

    # ---------- Nơi đăng (có tick chọn) ----------
    def find_target(self, url):
        return next((t for t in self.targets if t["url"] == url), None)

    def add_target(self, url, name="", checked=False):
        u = norm_url(url)
        if not u:
            return False
        name = clean_name(name)
        t = self.find_target(u)
        if t:
            if name and not t.get("name"):
                t["name"] = name
            return False
        self.targets.append({"url": u, "name": name, "checked": checked})
        return True

    def visible_targets(self):
        q = self.tfilter.get().strip().lower()
        k = self.kind_filter.get()
        return [t for t in self.targets
                if (k == "Tất cả" or kind_of(t["url"]) == k)
                and (not q or q in (t.get("name") or "").lower() or q in t["url"].lower())]

    def render_targets(self):
        sel = set(self.ttree.selection())
        self.ttree.delete(*self.ttree.get_children())
        for t in self.visible_targets():
            self.ttree.insert("", "end", iid=t["url"], values=(ON if t["checked"] else OFF, kind_of(t["url"]),
                                                              t.get("name") or "(chưa có tên)", short(t["url"])))
        keep = [i for i in sel if self.ttree.exists(i)]
        if keep:
            self.ttree.selection_set(keep)
        n = sum(t["checked"] for t in self.targets)
        ng = sum(kind_of(t["url"]) == "Group" for t in self.targets)
        self.tcount.config(text=f"Đã chọn {n}/{len(self.targets)}  ·  {ng} group · {len(self.targets) - ng} trang"
                                "   (bấm vào ô ☐ để chọn, hoặc chọn nhiều dòng rồi nhấn phím Space)")

    def on_target_click(self, e):
        if self.ttree.identify_region(e.x, e.y) == "heading":
            if self.ttree.identify_column(e.x) == "#1":
                vis = self.visible_targets()
                self.check_all(not all(t["checked"] for t in vis))
            return
        iid = self.ttree.identify_row(e.y)
        if iid and self.ttree.identify_column(e.x) == "#1":
            t = self.find_target(iid)
            t["checked"] = not t["checked"]
            self.render_targets()
            self.save()
            return "break"

    def toggle_selected(self):
        sel = [self.find_target(i) for i in self.ttree.selection()]
        if sel:
            v = not all(t["checked"] for t in sel)
            for t in sel:
                t["checked"] = v
            self.render_targets()
            self.save()
        return "break"

    def check_all(self, v):
        """Áp dụng cho các dòng đang hiển thị (theo bộ lọc)."""
        for t in self.visible_targets():
            t["checked"] = v
        self.render_targets()
        self.save()

    def delete_targets(self):
        sel = set(self.ttree.selection())
        if not sel:
            return messagebox.showinfo("Xoá dòng", "Hãy chọn (bôi xanh) các dòng muốn xoá trước.")
        if messagebox.askyesno("Xoá dòng", f"Xoá {len(sel)} nơi đăng khỏi danh sách?"):
            self.targets = [t for t in self.targets if t["url"] not in sel]
            self.render_targets()
            self.save()

    def add_links(self):
        win = tk.Toplevel(self.root)
        win.title("Thêm link Group / Trang")
        win.geometry("560x320")
        win.transient(self.root)
        ttk.Label(win, text="Dán link (mỗi dòng 1 link, có thể ghi tên phía sau link):", padding=8).pack(anchor="w")
        box = tk.Text(win, height=10, relief="solid", borderwidth=1)
        box.pack(fill="both", expand=True, padx=8)
        box.focus_set()

        def ok():
            added = 0
            for line in box.get("1.0", "end").splitlines():
                parts = line.strip().split(None, 1)
                if parts:
                    added += self.add_target(parts[0], parts[1] if len(parts) > 1 else "", True)
            self.render_targets()
            self.save()
            self.status.config(text=f"Đã thêm {added} nơi đăng")
            win.destroy()

        ttk.Button(win, text="＋ Thêm", style="Accent.TButton", command=ok).pack(anchor="e", padx=8, pady=8)

    def merge_scan(self, kind, items):
        if not items:
            messagebox.showinfo("Quét", "Không tìm thấy link nào.\nKiểm tra đã đăng nhập Facebook trong cửa sổ Chrome của app chưa.")
            return
        new = sum(self.add_target(it["url"], it["name"], False) for it in items)
        self.kind_filter.set("Group" if kind == "groups" else "Trang")
        self.tfilter.set("")
        self.render_targets()
        self.save()
        self.status.config(text=f"Quét được {len(items)} {'group' if kind == 'groups' else 'trang'} ({new} mới)")
        messagebox.showinfo("Quét xong", f"Quét được {len(items)} {'group' if kind == 'groups' else 'trang'}, "
                                        f"trong đó {new} mới được thêm vào danh sách.\n\n"
                                        "Tick ☑ các nơi muốn đăng (hoặc bấm 'Chọn tất cả').")

    # =============================== HÀNH ĐỘNG ===============================
    def set_busy(self, on):
        self.busy = on
        s = "disabled" if on else "normal"
        for b in self.scan_btns + self.hbtns + [self.api_scan_btn]:
            b.config(state=s)
        self.paint_main_btn(on)
        has_err = bool(self.session) and any(r["status"] in RETRY_STATES for r in self.session["results"])
        self.retry_btn.config(state="normal" if (not on and has_err) else "disabled")

    def paint_main_btn(self, running):
        if hasattr(self, "api_btn"):
            if running:
                self.api_btn.config(text="■  DỪNG", bg="#e41e3f", activebackground="#c9172f", fg="white",
                                    activeforeground="white", state="normal")
            else:
                self.api_btn.config(text="▶  ĐĂNG BẰNG API LÊN CÁC TRANG ĐÃ CHỌN", bg="#1877f2",
                                    activebackground="#166fe5", fg="white", activeforeground="white", state="normal")
        if running:
            self.start_btn.config(text="■  DỪNG", bg="#e41e3f", activebackground="#c9172f", fg="white",
                                  activeforeground="white", state="normal")
        else:
            self.start_btn.config(text="▶  ĐĂNG LÊN CÁC NƠI ĐÃ CHỌN", bg="#1877f2", activebackground="#166fe5",
                                  fg="white", activeforeground="white", state="normal")

    def toggle_run(self):
        if self.busy:
            self.stop()
            for b in (self.start_btn, self.api_btn):
                b.config(text="⏳  Đang dừng…", state="disabled", bg="#8a8d91")
        else:
            self.start()

    def toggle_api_run(self):
        if self.busy:
            self.toggle_run()
        else:
            self.api_start()

    def open_browser(self):
        self.status.config(text="Đang mở Chrome…")
        self.engine.send("open")

    def scan(self, kind):
        if self.busy:
            return
        self.set_busy(True)
        self.engine.send("scan", kind)

    def start(self):
        chosen = [t for t in self.targets if t["checked"]]
        if not chosen:
            return messagebox.showwarning("FB Auto Poster", "Chưa tick chọn nơi đăng nào.")
        self.run(chosen, self.get_text(), list(self.media))

    def retry_current(self):
        s = self.session
        if s:
            self.rerun(s, [r for r in s["results"] if r["status"] in RETRY_STATES])

    def rerun(self, s, results):
        if s.get("api"):
            self.run_api(results, s["text"], s["media"], s.get("link", ""))
        else:
            self.run(results, s["text"], s["media"])

    def run(self, targets, text, media):
        media_ok = [m for m in media if os.path.exists(m)]
        if not text.strip() and not media_ok:
            return messagebox.showwarning("FB Auto Poster", "Chưa có nội dung hoặc ảnh/video.")
        if not targets:
            return messagebox.showwarning("FB Auto Poster", "Không có nơi nào để đăng.")
        if len(media_ok) < len(media) and not messagebox.askyesno(
                "Thiếu file", f"{len(media) - len(media_ok)} ảnh/video không còn trên máy. Vẫn đăng phần còn lại?"):
            return
        if self.auto.get() and not messagebox.askyesno(
                "Xác nhận", f"Tự động đăng lên {len(targets)} nơi, KHÔNG dừng lại kiểm tra.\nTiếp tục?"):
            return
        dmin = max(10, self.safe_int(self.dmin, 20))
        dmax = max(dmin, self.safe_int(self.dmax, 20))
        self.save()
        self.session = {
            "id": time.strftime("%Y%m%d-%H%M%S"), "start": time.strftime("%d/%m/%Y %H:%M"), "end": None,
            "text": text, "media": media_ok, "auto": self.auto.get(),
            "results": [{"url": t["url"], "name": t.get("name", ""), "status": "queue", "msg": ""} for t in targets],
        }
        self.history.append(self.session)
        self.save_history()
        self.render_results()
        self.set_busy(True)
        self.nb.select(0)
        self.engine.send("run", {"urls": [(i, r["url"]) for i, r in enumerate(self.session["results"])],
                                 "text": text, "media": media_ok, "dmin": dmin, "dmax": dmax, "auto": self.auto.get()})

    def stop(self):
        self.engine.stop()
        self.api_stop.set()
        self.status.config(text="Đang dừng…")

    @staticmethod
    def fill_result_tree(tree, results):
        tree.delete(*tree.get_children())
        for r in results:
            tree.insert("", "end", values=(ICON.get(r["status"], ""), r.get("name") or short(r["url"]), r["msg"]))

    @staticmethod
    def count(results):
        c = lambda *s: sum(r["status"] in s for r in results)
        return c("posted"), c("pending"), c("error", "skipped", "stopped")

    def render_results(self):
        res = self.session["results"] if self.session else []
        self.fill_result_tree(self.rtree, res)
        ok, pend, bad = self.count(res)
        self.summary.config(text=f"✅ {ok} · 🕓 {pend} · ❌/⏭ {bad} / {len(res)}" if res else "")
        api_res = res if self.session and self.session.get("api") else []
        self.fill_result_tree(self.api_rtree, api_res)
        self.api_summary.config(text=f"✅ {ok} · ❌ {bad} / {len(api_res)}" if api_res else "")

    # ---------- Đăng Trang bằng API ----------
    def on_tab_changed(self):
        cur = self.nb.select()
        if cur == self.hist_tab:
            self.render_history()
        elif cur == self.api_tab:
            self.render_api_content()

    def render_api_content(self):
        text = " ".join(self.get_text().split())
        media = [m for m in self.media if os.path.exists(m)]
        nv = sum(map(is_video, media))
        self.api_content.config(text=(
            f"Nội dung lấy từ tab Đăng bài: {len(self.get_text())} ký tự · {len(media) - nv} ảnh · {nv} video\n"
            f"“{text[:160]}{'…' if len(text) > 160 else ''}”" if text or media
            else "Chưa có nội dung — soạn ở tab ✍ Đăng bài."))

    def render_api_pages(self):
        self.api_ptree.delete(*self.api_ptree.get_children())
        for p in self.api_pages:
            self.api_ptree.insert("", "end", iid=p["id"], values=(ON if p["checked"] else OFF, p["name"], p["id"],
                                                                  short(p["link"])))
        n = sum(p["checked"] for p in self.api_pages)
        self.api_pcount.config(text=f"Đã chọn {n}/{len(self.api_pages)} trang" if self.api_pages else
                               "Chưa có trang — dán token rồi bấm 'Quét trang bằng API'")

    def find_api_page(self, pid):
        return next((p for p in self.api_pages if p["id"] == pid), None)

    def on_api_page_click(self, e):
        if self.api_ptree.identify_region(e.x, e.y) == "heading":
            if self.api_ptree.identify_column(e.x) == "#1":
                self.api_check_all(not all(p["checked"] for p in self.api_pages))
            return
        iid = self.api_ptree.identify_row(e.y)
        if iid and self.api_ptree.identify_column(e.x) == "#1":
            p = self.find_api_page(iid)
            p["checked"] = not p["checked"]
            self.render_api_pages()
            self.save()
            return "break"

    def api_toggle_selected(self):
        sel = [self.find_api_page(i) for i in self.api_ptree.selection()]
        if sel:
            v = not all(p["checked"] for p in sel)
            for p in sel:
                p["checked"] = v
            self.render_api_pages()
            self.save()
        return "break"

    def api_check_all(self, v):
        for p in self.api_pages:
            p["checked"] = v
        self.render_api_pages()
        self.save()

    def need_api(self):
        if fbapi is None:
            messagebox.showerror("Đăng Trang bằng API", "Thiếu thư viện 'requests'.\n"
                                 "Mở Command Prompt và chạy:  pip install requests\nrồi mở lại app.")
            return False
        if not self.api_token.get().strip():
            if messagebox.askyesno("Chưa có token", "Chưa dán Access token. Mở trang lấy token ngay?"):
                webbrowser.open(fbapi.TOKEN_URL)
            return False
        return True

    def api_scan(self):
        if self.busy or not self.need_api():
            return
        token = self.api_token.get().strip()
        self.save()
        self.api_scan_btn.config(state="disabled")
        self.status.config(text="Đang quét trang bằng API…")

        def work():
            try:
                me = fbapi.get_me(token)
                pages = fbapi.list_pages(token)
            except Exception as e:
                self.api_events.put(("api_scan_error", str(e)))
                return
            self.api_events.put(("api_scan", me.get("name", ""), pages))
        threading.Thread(target=work, daemon=True).start()

    def api_merge_scan(self, who, pages):
        self.api_scan_btn.config(state="normal")
        old = {p["id"]: p["checked"] for p in self.api_pages}
        self.api_pages = [{"id": p["id"], "name": p["name"], "link": p["link"], "checked": old.get(p["id"], False)}
                          for p in pages]
        new = sum(p["id"] not in old for p in pages)
        self.api_who.config(text=f"Token của: {who}" if who else "")
        self.render_api_pages()
        self.save()
        self.status.config(text=f"API: quét được {len(pages)} trang ({new} mới)")
        if not pages:
            messagebox.showinfo("Quét trang bằng API", "Token này không quản lý trang nào.\nKhi bấm Generate token, "
                                "nhớ chọn các trang muốn cho phép đăng.")

    def api_start(self):
        chosen = [{"url": p["link"], "name": p["name"], "page_id": p["id"]} for p in self.api_pages if p["checked"]]
        if not chosen:
            return messagebox.showwarning("Đăng Trang bằng API", "Chưa tick chọn trang nào.")
        self.run_api(chosen, self.get_text(), list(self.media), self.api_link.get().strip())

    def run_api(self, targets, text, media, link=""):
        if self.busy or not self.need_api():
            return
        media_ok = [m for m in media if os.path.exists(m)]
        if not text.strip() and not media_ok and not link:
            return messagebox.showwarning("Đăng Trang bằng API", "Chưa có nội dung, link hoặc ảnh/video.")
        if not targets:
            return messagebox.showwarning("Đăng Trang bằng API", "Không có trang nào để đăng.")
        if len(media_ok) < len(media) and not messagebox.askyesno(
                "Thiếu file", f"{len(media) - len(media_ok)} ảnh/video không còn trên máy. Vẫn đăng phần còn lại?"):
            return
        nv = sum(map(is_video, media_ok))
        if nv and (nv > 1 or len(media_ok) > nv or link) and not messagebox.askyesno(
                "Bài video", "Qua API, bài video chỉ đăng được 1 video, không kèm ảnh/link.\n"
                             "App sẽ đăng video đầu tiên và bỏ qua phần còn lại. Tiếp tục?"):
            return
        if not messagebox.askyesno("Xác nhận", f"Đăng bằng API lên {len(targets)} trang ngay (không duyệt tay)?"):
            return
        dmin = max(10, self.safe_int(self.dmin, 20))
        dmax = max(dmin, self.safe_int(self.dmax, 20))
        self.save()
        self.session = {
            "id": time.strftime("%Y%m%d-%H%M%S"), "start": time.strftime("%d/%m/%Y %H:%M"), "end": None,
            "text": text, "media": media_ok, "auto": True, "api": True, "link": link,
            "results": [{"url": t["url"], "name": t.get("name", ""), "page_id": t.get("page_id") or t.get("id"),
                         "status": "queue", "msg": ""} for t in targets],
        }
        self.history.append(self.session)
        self.save_history()
        self.render_results()
        self.set_busy(True)
        self.api_stop.clear()
        jobs = [(i, r["page_id"]) for i, r in enumerate(self.session["results"])]
        threading.Thread(target=self.api_worker, daemon=True,
                         args=(self.api_token.get().strip(), jobs, text, media_ok, link, dmin, dmax)).start()

    def api_worker(self, token, jobs, text, media, link, dmin, dmax):
        """Chạy ở luồng nền; báo về giao diện qua self.api_events (cùng dạng sự kiện với Chrome)."""
        emit, stopped = self.api_events.put, self.api_stop.is_set
        emit(("status", "API: đang lấy quyền đăng của các trang…"))
        try:
            pages = {p["id"]: p for p in fbapi.list_pages(token)}
        except Exception as e:
            emit(("done", f"Lỗi: không dùng được token — {e}"))
            return
        n = len(jobs)
        for i, (idx, pid) in enumerate(jobs):
            if stopped():
                break
            emit(("status", f"API: đang đăng {i + 1}/{n}"))
            emit(("result", idx, "run", "Đang tải lên…"))
            try:
                page = pages.get(pid)
                if not page:
                    raise fbapi.GraphError("Token này không còn quyền với Trang này")
                post_id, note = fbapi.post_to_page(page, text, media, link, stop=stopped)
                status, msg = "posted", f"{note} — https://www.facebook.com/{post_id}"
            except Exception as e:
                status = "stopped" if stopped() else "error"
                msg = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            emit(("result", idx, status, msg))
            if i < n - 1 and status == "posted":
                for s in range(random.randint(dmin, dmax), 0, -1):
                    if stopped():
                        break
                    emit(("status", f"API: nghỉ {s}s trước trang tiếp theo"))
                    time.sleep(1)
        emit(("done", "Đã dừng" if stopped() else "Hoàn tất"))

    def open_result_link(self, e):
        iid = e.widget.identify_row(e.y)
        m = URL_RE.search(e.widget.item(iid, "values")[2]) if iid else None
        if m:
            webbrowser.open(m.group(0))

    # ---------- Lịch sử ----------
    def render_history(self):
        sel = self.htree.selection()
        self.htree.delete(*self.htree.get_children())
        for s in reversed(self.history):
            ok, pend, bad = self.count(s["results"])
            first = " ".join(s["text"].split())[:90] or f"({len(s['media'])} ảnh/video)"
            self.htree.insert("", "end", iid=s["id"], values=(
                s["start"] + ("  ⏳" if not s.get("end") else ""), first, len(s["results"]), ok, pend, bad,
                "API" if s.get("api") else "Tự động" if s.get("auto") else "Duyệt tay"))
        if sel and self.htree.exists(sel[0]):
            self.htree.selection_set(sel[0])

    def selected_session(self):
        sel = self.htree.selection()
        return next((s for s in self.history if s["id"] == sel[0]), None) if sel else None

    def show_session_detail(self):
        s = self.selected_session()
        self.htext.config(state="normal")
        self.htext.delete("1.0", "end")
        if s:
            self.htext.insert("1.0", s["text"])
        self.htext.config(state="disabled")
        self.hmedia.config(text=("Ảnh/video: " + ", ".join(os.path.basename(m) for m in s["media"])) if s and s["media"] else "")
        self.fill_result_tree(self.hrtree, s["results"] if s else [])

    def load_session_content(self):
        s = self.selected_session()
        if not s:
            return messagebox.showinfo("Lịch sử", "Chọn 1 phiên trong danh sách trước.")
        if self.get_text().strip() and not messagebox.askyesno("Nạp nội dung", "Thay nội dung đang soạn bằng nội dung của phiên này?"):
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", s["text"])
        self.media = list(s["media"])
        if s.get("api"):
            self.api_link.set(s.get("link", ""))
        self.render_media()
        self.chars.config(text=f"{len(self.get_text())} ký tự")
        self.save()
        self.nb.select(0)
        self.status.config(text=f"Đã nạp nội dung phiên {s['start']}")

    def retry_session(self):
        s = self.selected_session()
        if not s:
            return messagebox.showinfo("Lịch sử", "Chọn 1 phiên trong danh sách trước.")
        bad = [r for r in s["results"] if r["status"] in RETRY_STATES]
        if not bad:
            return messagebox.showinfo("Lịch sử", "Phiên này không có nơi nào bị lỗi.")
        self.rerun(s, bad)

    def delete_session(self):
        s = self.selected_session()
        if s and messagebox.askyesno("Xoá phiên", f"Xoá phiên đăng lúc {s['start']} khỏi lịch sử?"):
            self.history.remove(s)
            if self.session is s:
                self.session = None
            self.save_history()
            self.render_history()
            self.show_session_detail()

    # =============================== SỰ KIỆN TỪ ENGINE ===============================
    def poll(self):
        for q in (self.engine.events, self.api_events):
            try:
                while True:
                    self.handle_event(q.get_nowait())
            except queue.Empty:
                pass
        self.root.after(200, self.poll)

    def handle_event(self, ev):
        kind = ev[0]
        if kind == "status":
            self.status.config(text=ev[1])
        elif kind == "result" and self.session:
            _, idx, st, msg = ev
            if idx < len(self.session["results"]):
                self.session["results"][idx].update(status=st, msg=msg)
                self.render_results()
                if st != "run":
                    self.save_history()
        elif kind == "who":
            _, name, is_page, url = ev
            t = self.find_target(norm_url(url) or "")
            name = (t and t.get("name")) or name or short(url)
            self.who.config(text=f"Đang đăng với: {name}  ({'TRANG' if is_page else 'tài khoản cá nhân'})",
                            foreground="#c27c0e" if is_page else "#31a24c")
        elif kind == "scan":
            self.merge_scan(ev[1], ev[2])
        elif kind == "done":
            if self.session and not self.session.get("end"):
                for r in self.session["results"]:
                    if r["status"] in ("queue", "run"):
                        r.update(status="stopped", msg="Chưa chạy (đã dừng)")
                self.session["end"] = time.strftime("%d/%m/%Y %H:%M")
                self.save_history()
                self.render_results()
            self.set_busy(False)
            self.render_history()
            self.status.config(text=ev[1])
            if ev[1].startswith("Lỗi"):
                messagebox.showerror("FB Auto Poster", ev[1])
        elif kind == "api_scan":
            self.api_merge_scan(ev[1], ev[2])
        elif kind == "api_scan_error":
            self.api_scan_btn.config(state="normal")
            self.status.config(text="Quét trang bằng API bị lỗi")
            messagebox.showerror("Quét trang bằng API", f"{ev[1]}\n\nToken có thể đã hết hạn hoặc thiếu quyền "
                                 "pages_show_list / pages_manage_posts. Bấm 'Mở link lấy token' để lấy token mới.")

    def on_close(self):
        if self.busy and not messagebox.askyesno("Thoát", "App đang chạy. Thoát luôn?"):
            return
        try:
            self.save()
            if self.session and not self.session.get("end"):
                self.session["end"] = time.strftime("%d/%m/%Y %H:%M")
                for r in self.session["results"]:
                    if r["status"] in ("queue", "run"):
                        r.update(status="stopped", msg="App bị tắt khi đang chạy")
            self.save_history()
        except Exception:
            pass
        self.root.destroy()
        os._exit(0)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

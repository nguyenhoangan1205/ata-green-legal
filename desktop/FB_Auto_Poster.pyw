"""FB Auto Poster — ứng dụng desktop đăng bài lên nhiều Group/Trang Facebook.
Mở bằng file "Mo FB Auto Poster.bat" (hoặc nhấp đúp file này)."""
import json
import os
import queue
import re
import sys
import time
import tkinter as tk
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

ICON = {"queue": "⏸", "run": "⏳", "posted": "✅", "pending": "🕓", "skipped": "⏭", "error": "❌", "stopped": "■"}
RETRY_STATES = ("error", "skipped", "stopped", "queue", "run")
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
        hist_tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(post_tab, text="  ✍  Đăng bài  ")
        self.nb.add(hist_tab, text="  🕘  Lịch sử phiên đăng  ")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.render_history() if self.nb.index("current") == 1 else None)
        self.build_post_tab(post_tab)
        self.build_history_tab(hist_tab)
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
               "dmin": self.safe_int(self.dmin, 20), "dmax": self.safe_int(self.dmax, 20), "auto": self.auto.get()}
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
        for b in self.scan_btns + self.hbtns:
            b.config(state=s)
        self.paint_main_btn(on)
        has_err = bool(self.session) and any(r["status"] in RETRY_STATES for r in self.session["results"])
        self.retry_btn.config(state="normal" if (not on and has_err) else "disabled")

    def paint_main_btn(self, running):
        if running:
            self.start_btn.config(text="■  DỪNG", bg="#e41e3f", activebackground="#c9172f", fg="white",
                                  activeforeground="white", state="normal")
        else:
            self.start_btn.config(text="▶  ĐĂNG LÊN CÁC NƠI ĐÃ CHỌN", bg="#1877f2", activebackground="#166fe5",
                                  fg="white", activeforeground="white", state="normal")

    def toggle_run(self):
        if self.busy:
            self.stop()
            self.start_btn.config(text="⏳  Đang dừng…", state="disabled", bg="#8a8d91")
        else:
            self.start()

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
            self.run([r for r in s["results"] if r["status"] in RETRY_STATES], s["text"], s["media"])

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

    # ---------- Lịch sử ----------
    def render_history(self):
        sel = self.htree.selection()
        self.htree.delete(*self.htree.get_children())
        for s in reversed(self.history):
            ok, pend, bad = self.count(s["results"])
            first = " ".join(s["text"].split())[:90] or f"({len(s['media'])} ảnh/video)"
            self.htree.insert("", "end", iid=s["id"], values=(
                s["start"] + ("  ⏳" if not s.get("end") else ""), first, len(s["results"]), ok, pend, bad,
                "Tự động" if s.get("auto") else "Duyệt tay"))
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
        self.run(bad, s["text"], s["media"])

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
        try:
            while True:
                ev = self.engine.events.get_nowait()
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
        except queue.Empty:
            pass
        self.root.after(200, self.poll)

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

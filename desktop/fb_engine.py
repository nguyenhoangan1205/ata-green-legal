"""Bộ máy điều khiển trình duyệt riêng (Playwright) để đăng bài lên Facebook.

Chạy trong 1 luồng riêng; giao diện gửi lệnh qua `engine.send(...)` và nhận sự kiện qua `engine.events`.
Sự kiện: ("status", text) | ("log", text) | ("result", idx, status, msg) | ("scan", kind, [ {url,name} ]) | ("done", text)
"""
import os
import queue
import random
import subprocess
import threading
import time
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------- JavaScript chạy trong trang Facebook ----------------

JS_OPEN_COMPOSER = r"""
async (text) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitFor = async (fn, timeout = 15000, step = 300) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) { try { const v = fn(); if (v) return v; } catch (e) {} await sleep(step); }
    return null;
  };
  const norm = (s) => (s || "").normalize("NFC").toLowerCase().replace(/\s+/g, " ").trim();
  const visible = (el) => !!el && el.getClientRects().length > 0;
  const labelOf = (el) => norm(el.getAttribute("aria-label") || el.innerText);

  if (document.querySelector('input[name="pass"]') || /\/(login|checkpoint)/.test(location.pathname))
    return { ok: false, msg: "Chưa đăng nhập Facebook (bấm 'Mở trình duyệt' để đăng nhập)" };

  document.querySelectorAll("[data-fbp]").forEach((e) => e.removeAttribute("data-fbp"));
  const TRIGGERS = ["bạn viết gì đi", "viết gì đó", "write something", "bạn đang nghĩ gì", "what's on your mind",
                    "tạo bài viết công khai", "create a public post", "tạo bài viết", "create post"];
  const findTrigger = () => [...document.querySelectorAll('[role="button"]')].find((b) => {
    if (!visible(b) || b.closest('[role="dialog"]')) return false;
    const t = labelOf(b);
    return t.length < 80 && TRIGGERS.some((k) => t.startsWith(k));
  });
  // Không có ô đăng bài -> báo lý do ngay để chuyển sang nơi tiếp theo (không chờ lâu)
  const blockedReason = () => {
    const labels = [...document.querySelectorAll('[role="button"]')].filter(visible).map(labelOf);
    if (labels.some((t) => /^(hủy yêu cầu|huỷ yêu cầu|cancel request|đã gửi yêu cầu)/.test(t)))
      return "Đang chờ admin duyệt cho vào nhóm — bỏ qua";
    if (labels.some((t) => /^(tham gia nhóm|join group)/.test(t)))
      return "CHƯA THAM GIA nhóm này — bỏ qua";
    if (labels.some((t) => /^(bán gì đó|sell something)/.test(t)))
      return "Nhóm mua bán: chỉ cho đăng tin niêm yết, không có ô đăng bài thường — bỏ qua";
    // Bảng tin đã hiện mà vẫn không có ô đăng bài -> không có quyền đăng
    if (document.querySelector('[role="feed"], [role="article"]'))
      return "Không có ô đăng bài (nhóm/trang không cho đăng) — bỏ qua";
    return null;
  };
  const t0 = Date.now();
  let trigger = await waitFor(() => findTrigger() || (Date.now() - t0 > 2500 && blockedReason() ? "blocked" : null), 8000);
  if (!trigger) { window.scrollTo(0, 500); trigger = await waitFor(findTrigger, 1500); }
  if (!trigger || trigger === "blocked")
    return { ok: false, skip: true, msg: blockedReason() || "Không có ô đăng bài (không có quyền đăng) — bỏ qua" };
  trigger.scrollIntoView({ block: "center" });
  await sleep(400);
  trigger.click();

  const getDialog = () => [...document.querySelectorAll('[role="dialog"]')].reverse()
    .find((d) => visible(d) && d.querySelector('[contenteditable="true"][role="textbox"]'));
  const dlg = await waitFor(getDialog, 15000);
  if (!dlg) return { ok: false, msg: "Hộp soạn bài không mở ra" };
  dlg.setAttribute("data-fbp", "dialog");
  const box = dlg.querySelector('[contenteditable="true"][role="textbox"]');
  box.setAttribute("data-fbp", "box");
  box.focus();
  await sleep(600);

  if (text) {
    const probe = norm(text).slice(0, 15);
    const dt = new DataTransfer();
    dt.setData("text/plain", text);
    box.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    await sleep(900);
    if (!norm(box.innerText).includes(probe)) { document.execCommand("insertText", false, text); await sleep(900); }
    if (!norm(box.innerText).includes(probe)) return { ok: true, typed: false };
  }
  const photo = [...dlg.querySelectorAll('[role="button"],[aria-label]')]
    .find((b) => /ảnh\/video|photo\/video/.test(labelOf(b)));
  if (photo) photo.setAttribute("data-fbp", "photo");
  return { ok: true, typed: true };
}
"""

# Tìm nút Đăng (hoặc bấm "Tiếp" nếu có). Trả về "ready" | "next" | "wait" | "gone"
JS_FIND_POST = r"""
() => {
  const norm = (s) => (s || "").normalize("NFC").toLowerCase().replace(/\s+/g, " ").trim();
  const visible = (el) => !!el && el.getClientRects().length > 0;
  const dlg = [...document.querySelectorAll('[role="dialog"]')].reverse().find((d) => visible(d) &&
              (d.querySelector('[data-fbp="box"]') || d.querySelector('[contenteditable="true"][role="textbox"]')));
  if (!dlg) return "gone";
  const btns = [...dlg.querySelectorAll('[role="button"]')].filter((b) => visible(b) && b.getAttribute("aria-disabled") !== "true");
  const lab = (b) => norm(b.getAttribute("aria-label") || b.innerText);
  const post = btns.find((b) => ["đăng", "post"].includes(lab(b)));
  if (post) { document.querySelectorAll('[data-fbp="post"]').forEach((e) => e.removeAttribute("data-fbp")); post.setAttribute("data-fbp", "post"); return "ready"; }
  const next = btns.find((b) => ["tiếp", "next"].includes(lab(b)));
  if (next) { next.click(); return "next"; }
  return "wait";
}
"""

JS_COMPOSER_GONE = r"""
() => { const b = document.querySelector('[data-fbp="box"]'); return !b || b.getClientRects().length === 0; }
"""

JS_MANUAL_PREP = r"""
() => {
  window.__fbpClicked = false;
  const p = document.querySelector('[data-fbp="post"]');
  if (p) {
    p.addEventListener("click", () => { window.__fbpClicked = true; }, true);
    p.style.outline = "4px solid #e41e3f"; p.style.outlineOffset = "2px";
  }
  const b = document.createElement("div");
  b.id = "__fbpBanner";
  b.textContent = "👉 FB Auto Poster: kiểm tra bài rồi bấm ĐĂNG (hoặc đóng hộp để bỏ qua nơi này)";
  b.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#e41e3f;color:#fff;font:600 15px system-ui;padding:10px;text-align:center;pointer-events:none";
  document.body.appendChild(b);
}
"""

JS_MANUAL_DONE = r"""
() => { document.getElementById("__fbpBanner")?.remove(); return !!window.__fbpClicked; }
"""

JS_PENDING = r"""
() => /chờ.{0,30}(phê duyệt|duyệt)|pending|awaiting approval/i.test(
  [...document.querySelectorAll('[role="alert"],[role="status"],[role="dialog"]')].map((e) => e.innerText).join(" "))
"""

JS_SCAN = r"""
async (kind) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const found = new Map();
  const SKIP_GROUP = new Set(["joins", "feed", "discover", "create", "notifications", "search", "you", "categories", "requests", "invites"]);
  const SKIP_PAGE = /^\/(pages|groups|watch|marketplace|gaming|events|notifications|messages|settings|help|policies|privacy|bookmarks|friends|business|ads|reel|reels|stories|photo|photos|hashtag|search|saved|memories|fundraisers|login|latest|home\.php)\b/i;
  const collect = () => {
    const root = document.querySelector('[role="main"]') || document.body;
    for (const a of root.querySelectorAll("a[href]")) {
      let u; try { u = new URL(a.href); } catch (e) { continue; }
      if (!/(^|\.)facebook\.com$/.test(u.hostname)) continue;
      const name = (a.innerText || a.getAttribute("aria-label") || "").trim().split("\n")[0]
        .replace(/^(ảnh đại diện của|profile picture of|trang cá nhân của)\s*/i, "").slice(0, 100);
      let key = null, url = null;
      if (kind === "groups") {
        const m = u.pathname.match(/^\/groups\/([^/]+)\/?$/);
        if (m && !SKIP_GROUP.has(m[1])) { key = m[1]; url = `https://www.facebook.com/groups/${m[1]}`; }
      } else if (u.pathname === "/profile.php" && u.searchParams.get("id")) {
        key = u.searchParams.get("id"); url = `https://www.facebook.com/profile.php?id=${key}`;
      } else if (/^\/[^/]+\/?$/.test(u.pathname) && !SKIP_PAGE.test(u.pathname)) {
        key = u.pathname.replace(/\//g, ""); url = `https://www.facebook.com/${key}`;
      }
      if (!key) continue;
      const old = found.get(key);
      if (!old || (!old.name && name)) found.set(key, { url, name });
    }
  };
  let still = 0, last = -1;
  for (let i = 0; i < 60 && still < 3; i++) {
    collect();
    if (found.size === last) still++; else { still = 0; last = found.size; }
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(1500);
  }
  collect();
  return [...found.values()].filter((x) => x.name);
}
"""

SCAN_URL = {
    "groups": "https://www.facebook.com/groups/joins/?nav_source=tab",
    "pages": "https://www.facebook.com/pages/?category=your_pages",
}

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv", ".3gp")


def is_video(path):
    return path.lower().endswith(VIDEO_EXT)


CDP_PORT = 9333


def find_chrome():
    cands = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                k = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                cands.insert(0, winreg.QueryValue(k, None))
            except OSError:
                pass
    except ImportError:
        pass
    for c in cands:
        if c and os.path.exists(c):
            return c
    raise RuntimeError("Không tìm thấy Google Chrome trên máy")


def launch_chrome(profile_dir, url="https://www.facebook.com/"):
    """Mở Chrome thật với hồ sơ riêng của FB Auto Poster + cổng gỡ lỗi để app điều khiển."""
    os.makedirs(profile_dir, exist_ok=True)
    subprocess.Popen([find_chrome(), f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile_dir}",
                      "--no-first-run", "--no-default-browser-check", "--start-maximized", url],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                     close_fds=True)


class Stopped(Exception):
    pass


class Engine(threading.Thread):
    def __init__(self, profile_dir):
        super().__init__(daemon=True)
        self.profile_dir = profile_dir
        self.cmds = queue.Queue()
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.pw = None
        self.browser = None
        self.ctx = None
        self.work_page = None

    # ----- API cho giao diện -----
    def send(self, *cmd):
        self.cmds.put(cmd)

    def stop(self):
        self.stop_event.set()

    def emit(self, *ev):
        self.events.put(ev)

    # ----- Trình duyệt -----
    # App gắn vào cửa sổ Chrome thật đang mở (qua cổng gỡ lỗi CDP_PORT) thay vì tự mở trình duyệt tự động.
    # Nếu chưa có cửa sổ đó thì app mở Chrome với hồ sơ FB Auto Poster, và Chrome này vẫn giữ nguyên khi tắt app.
    def page(self):
        if self.browser is not None:
            try:
                if self.work_page is not None and not self.work_page.is_closed():
                    return self.work_page
                self.work_page = self.ctx.new_page()
                return self.work_page
            except Exception:
                self.browser = self.ctx = self.work_page = None
        if self.pw is None:
            self.pw = sync_playwright().start()
        if not self._cdp_ready():
            launch_chrome(self.profile_dir)
            end = time.time() + 30
            while not self._cdp_ready():
                if time.time() > end:
                    raise RuntimeError("Không kết nối được Chrome. Hãy tắt cửa sổ 'Chrome FB Auto' rồi thử lại.")
                time.sleep(0.5)
        self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        self.browser.on("disconnected", lambda *_: setattr(self, "browser", None))
        self.ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        # Dùng lại tab trắng / tab Facebook nếu có, không đụng tới các tab khác của bạn
        pages = self.ctx.pages
        reuse = [p for p in pages if "facebook.com" in p.url] or                 [p for p in pages if p.url in ("about:blank", "chrome://newtab/", "chrome://new-tab-page/")]
        self.work_page = reuse[-1] if reuse else self.ctx.new_page()
        return self.work_page

    @staticmethod
    def _cdp_ready():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1) as r:
                return r.status == 200
        except Exception:
            return False

    def goto(self, url):
        p = self.page()
        p.bring_to_front()
        p.goto(url, wait_until="domcontentloaded", timeout=60000)
        p.wait_for_timeout(3500)
        return p

    def whoami(self):
        """Tên tài khoản / Trang mà Facebook đang dùng để đăng."""
        p = self.goto("https://www.facebook.com/me")
        name = p.evaluate("""() => { const h = document.querySelector('[role="main"] h1') || document.querySelector('h1');
                                     return h ? h.innerText.trim().split('\\n')[0] : ''; }""") or ""
        if name.lower() in ("quản lý trang", "manage page", "facebook"):
            name = ""
        self.emit("who", name, "profile.php" in p.url, p.url.split("&")[0].rstrip("/"))
        return name

    def wait(self, page, ms):
        """Chờ nhưng vẫn kiểm tra nút Dừng."""
        end = time.time() + ms / 1000
        while time.time() < end:
            if self.stop_event.is_set():
                raise Stopped()
            page.wait_for_timeout(min(500, max(1, int((end - time.time()) * 1000))))

    # ----- Vòng lặp lệnh -----
    def run(self):
        while True:
            cmd = self.cmds.get()
            self.stop_event.clear()
            try:
                if cmd[0] == "open":
                    self.whoami()
                    self.goto("https://www.facebook.com/")
                    self.emit("done", "Chrome đã mở. Kiểm tra đúng tài khoản/Trang muốn dùng để đăng.")
                elif cmd[0] == "scan":
                    kind = cmd[1]
                    self.emit("status", "Đang quét…")
                    p = self.goto(SCAN_URL[kind])
                    items = p.evaluate(JS_SCAN, kind)
                    self.emit("scan", kind, items)
                    self.emit("done", f"Quét được {len(items)} link")
                elif cmd[0] == "run":
                    self.run_job(cmd[1])
            except Stopped:
                self.emit("done", "Đã dừng")
            except Exception as e:
                self.emit("done", f"Lỗi: {e}")

    def run_job(self, job):
        urls, n = job["urls"], len(job["urls"])
        try:
            self.whoami()
        except Exception:
            pass
        for i, (idx, url) in enumerate(urls):
            if self.stop_event.is_set():
                raise Stopped()
            self.emit("status", f"Đang đăng {i + 1}/{n}")
            self.emit("result", idx, "run", "Đang xử lý…")
            try:
                status, msg = self.post_one(url, job)
            except Stopped:
                self.emit("result", idx, "stopped", "Đã dừng")
                raise
            except Exception as e:
                status, msg = "error", str(e).splitlines()[0][:200]
            self.emit("result", idx, status, msg)
            # Chỉ nghỉ sau khi thật sự đăng; nơi bị bỏ qua / lỗi thì chuyển ngay sang nơi tiếp theo
            if i < n - 1 and status in ("posted", "pending"):
                delay = random.randint(job["dmin"], max(job["dmin"], job["dmax"]))
                for s in range(delay, 0, -1):
                    if self.stop_event.is_set():
                        raise Stopped()
                    self.emit("status", f"Nghỉ {s}s trước nơi tiếp theo")
                    time.sleep(1)
        self.emit("done", "Hoàn tất")

    def post_one(self, url, job):
        p = self.goto(url)
        res = p.evaluate(JS_OPEN_COMPOSER, job["text"])
        if not res.get("ok"):
            return ("skipped" if res.get("skip") else "error"), res.get("msg", "Lỗi mở hộp soạn bài")
        if job["text"] and not res.get("typed"):
            p.locator('[data-fbp="box"]').click()
            p.keyboard.insert_text(job["text"])
            p.wait_for_timeout(800)

        media = [m for m in job["media"] if os.path.exists(m)]
        n_video = sum(1 for m in media if is_video(m))
        if media:
            self.attach(p, media, n_video > 0)
            self.wait(p, 2500 + 1500 * len(media) + 5000 * n_video)

        # Chờ nút Đăng sáng lên (video cần lâu hơn)
        deadline = time.time() + 60 + 240 * n_video
        state = "wait"
        while time.time() < deadline:
            state = p.evaluate(JS_FIND_POST)
            if state in ("ready", "gone"):
                break
            self.wait(p, 1500 if state == "next" else 1000)
        if state == "gone":
            return "error", "Hộp soạn bài bị đóng bất ngờ"
        if state != "ready":
            return "error", "Nút Đăng không bật (ảnh/video chưa tải xong?)"

        if job["auto"]:
            p.locator('[data-fbp="post"]').click()
            if not self.wait_gone(p, 60):
                return "error", "Đã bấm Đăng nhưng hộp soạn chưa đóng — kiểm tra lại"
            self.wait(p, 2500)
            return ("pending", "Đã gửi, chờ admin duyệt") if p.evaluate(JS_PENDING) else ("posted", "Đã đăng")

        p.evaluate(JS_MANUAL_PREP)
        self.emit("status", "Chờ bạn bấm ĐĂNG trong trình duyệt…")
        self.wait_gone(p, 600)
        clicked = p.evaluate(JS_MANUAL_DONE)
        if not clicked:
            return "skipped", "Bạn đã đóng hộp / hết giờ chờ — bỏ qua"
        self.wait(p, 2500)
        return ("pending", "Đã gửi, chờ admin duyệt") if p.evaluate(JS_PENDING) else ("posted", "Đã đăng (bạn bấm)")

    def wait_gone(self, p, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if p.evaluate(JS_COMPOSER_GONE):
                return True
            self.wait(p, 700)
        return False

    def attach(self, p, files, want_video):
        dlg = p.locator('[data-fbp="dialog"]')

        def pick_input():
            inputs = dlg.locator('input[type="file"]')
            cnt = inputs.count()
            if not cnt:
                return None
            for k in range(cnt):
                acc = (inputs.nth(k).get_attribute("accept") or "")
                if ("video" in acc) if want_video else ("image" in acc):
                    return inputs.nth(k)
            return inputs.first

        inp = pick_input()
        if inp is not None:
            inp.set_input_files(files)
            return
        photo = p.locator('[data-fbp="photo"]')
        if photo.count():
            try:
                with p.expect_file_chooser(timeout=6000) as fc:
                    photo.first.click()
                fc.value.set_files(files)
                return
            except PWTimeout:
                inp = pick_input()
                if inp is not None:
                    inp.set_input_files(files)
                    return
        raise RuntimeError("Không tìm thấy chỗ đính kèm ảnh/video")
